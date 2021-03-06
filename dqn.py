from numpy.core.fromnumeric import size
from numpy.lib.npyio import save
import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
import os
from glob import glob
import time
from skimage import transform
from skimage.color import rgb2gray
from torch.utils.tensorboard import SummaryWriter
import gym
from gym_sokoban.envs.sokoban_env import SokobanEnv
import argparse

''' Reference: https://blog.csdn.net/GhostintheCode/article/details/102530451'''


def preprocess_frame(frame):
    gray = rgb2gray(frame)  # 轉灰階
    normalized_frame = gray/255.0  # normalization
    resized_img = transform.resize(normalized_frame, [84, 84])
    return resized_img

class Net(nn.Module):
    def __init__(self, n_states, n_actions):
        super(Net, self).__init__()
        self.n_states = n_states
        self.n_actions = n_actions

        Conv1 = [nn.Conv2d(1, 32, kernel_size=8, stride=4, padding=1, bias=False),
                 nn.ReLU(True),
                 nn.BatchNorm2d(32)]
        Conv2 = [nn.Conv2d(32, 64, kernel_size=4, stride=2, padding=0, bias=False),
                 nn.ReLU(True),
                 nn.BatchNorm2d(64)]
        Conv3 = [nn.Conv2d(64, 128, kernel_size=4, stride=2, padding=0, bias=False),
                 nn.ReLU(True),
                 nn.BatchNorm2d(128)]
        self.conv1 = nn.Sequential(*Conv1)
        self.conv2 = nn.Sequential(*Conv2)
        self.conv3 = nn.Sequential(*Conv3)
        self.flatten = nn.Flatten()
        Linear = [torch.nn.Linear(3*3*128, 512),
                  nn.ReLU(True)]
        self.linear = nn.Sequential(*Linear)
        self.out = torch.nn.Linear(512, n_actions)

    def forward(self, input):
        # shape of input = [batch, 84 ,84]
        x = torch.unsqueeze(input, 3)
        x = x.permute(0, 3, 2, 1)  # shape of x = [1, 1, 84, 84]

        x = self.conv1(x)  # (1, 84, 84) -> (32, 20, 20)
        x = self.conv2(x)  # (32, 20, 20) -> (64, 9, 9)
        x = self.conv3(x)  # (64, 9, 9) -> (128, 3, 3)
        x = self.flatten(x)  # 128*3*3 = 1152
        x = self.linear(x)  # 1152 -> 512
        out = self.out(x)  # 512 -> 9
        return out


class DQN(object):
    def __init__(self, n_states, n_actions, batch_size, lr, epsilon, gamma, target_replace_iter, memory_capacity, use_gpu):
        self.eval_net, self.target_net = Net(
            n_states, n_actions), Net(n_states, n_actions)

        # 每個 memory 中的 experience 大小為 (state + next state + reward + action)
        self.memory = np.zeros((memory_capacity, n_states * 2 + 2))
        self.optimizer = torch.optim.Adam(self.eval_net.parameters(), lr=lr)
        self.loss_func = nn.MSELoss()
        self.memory_counter = 0
        self.learn_step_counter = 0  # 讓 target network 知道什麼時候要更新

        self.n_states = n_states
        self.n_actions = n_actions
        self.batch_size = batch_size
        self.lr = lr
        self.epsilon = epsilon
        self.gamma = gamma
        self.target_replace_iter = target_replace_iter
        self.memory_capacity = memory_capacity
        
        # Initialize writer (紀錄 rewards 的東西)
        self.exp_name = 'exp1' # 實驗名稱
        self.writer = SummaryWriter()

        self.use_gpu = use_gpu
        self.device = torch.device(
            "cuda:0" if torch.cuda.is_available() else "cpu")
        if self.use_gpu:
            self.eval_net, self.target_net = self.eval_net.to(
                self.device), self.target_net.to(self.device)

    def choose_action(self, state):
        state = torch.FloatTensor(preprocess_frame(state)).unsqueeze(0)
        if self.use_gpu:
            x = state.to(self.device)

        # epsilon-greedy
        if np.random.uniform() < self.epsilon:  # 隨機
            action = np.random.randint(0, self.n_actions)
        else:  # 根據現有 policy 做最好的選擇
            # 以現有 eval net 得出各個 action 的分數
            actions_value = self.eval_net(x).cpu()
            action = torch.max(actions_value, 0)[1].numpy()[0]  # 挑選最高分的 action

        return action
    
    def choose_action_test(self, state):
        state = torch.FloatTensor(preprocess_frame(state)).unsqueeze(0)
        if self.use_gpu:
            x = state.to(self.device)
        
        actions_value = self.eval_net(x).cpu()
        action = torch.max(actions_value, 0)[1].numpy()[0]  # 挑選最高分的 action

        return action

    def store_transition(self, state, action, reward, next_state):
        # 打包 experience
        state = torch.FloatTensor(preprocess_frame(state)).view(-1)
        next_state = torch.FloatTensor(preprocess_frame(next_state)).view(-1)
        transition = np.hstack((state, [action, reward], next_state))

        # 存進 memory；舊 memory 可能會被覆蓋
        index = self.memory_counter % self.memory_capacity
        self.memory[index, :] = transition
        self.memory_counter += 1

    def learn(self):
        # 隨機取樣 batch_size 個 experience
        sample_index = np.random.choice(self.memory_capacity, self.batch_size)
        b_memory = self.memory[sample_index, :]
        b_state = torch.FloatTensor(b_memory[:, :self.n_states])
        b_action = torch.LongTensor(
            b_memory[:, self.n_states:self.n_states+1].astype(int))
        b_reward = torch.FloatTensor(
            b_memory[:, self.n_states+1:self.n_states+2])
        b_next_state = torch.FloatTensor(b_memory[:, -self.n_states:])

        if self.use_gpu:
            b_state = b_state.to(self.device)
            b_action = b_action.to(self.device)
            b_reward = b_reward.to(self.device)
            b_next_state = b_next_state.to(self.device)

        b_state, b_next_state = b_state.view(-1,
                                             84, 84), b_next_state.view(-1, 84, 84)

        # 計算現有 eval net 和 target net 得出 Q value 的落差
        # 重新計算這些 experience 當下 eval net 所得出的 Q value
        q_eval = self.eval_net(b_state).gather(1, b_action)
        # detach 才不會訓練到 target net
        q_next = self.target_net(b_next_state).detach()
        # 計算這些 experience 當下 target net 所得出的 Q value
        q_target = b_reward + self.gamma * \
            q_next.max(1)[0].view(self.batch_size, 1)
        loss = self.loss_func(q_eval, q_target)

        # Back propagation
        self.optimizer.zero_grad()
        loss.backward()
        self.optimizer.step()

        # 每隔一段時間 (target_replace_iter), 更新 target net，即複製 eval net 到 target net
        self.learn_step_counter += 1
        if self.learn_step_counter % self.target_replace_iter == 0:
            self.target_net.load_state_dict(self.eval_net.state_dict())

    def save(self, dir, step):
        torch.save(self.target_net.state_dict(), os.path.join(
            dir, 'Sokoban' + '_params_%05d.pt' % step))

    def load(self, dir, step):
        params = torch.load(os.path.join(
            dir, 'Sokoban' + '_params_%05d.pt' % step))
        self.target_net.load_state_dict(params)
        self.eval_net.load_state_dict(params)

def str2bool(x):
    return x.lower() in ('true')

def parse_args():
    desc = "DQN"
    parser = argparse.ArgumentParser(description=desc)
    parser.add_argument('--phase', type=str, default='train', help='[train / test]')
    parser.add_argument('--use_gpu', type=str2bool, default=True, help='Use GPU?')
    parser.add_argument('--load_model', type=str2bool, default=False, help='Load pretrained model?')
    parser.add_argument('--model_dir', type=str, default='trained_models', help='The directory to store models')

    return parser.parse_args()

'''------- Main -------'''

env = SokobanEnv()

# args
args = parse_args()
if args is None:
    exit()
use_gpu = args.use_gpu
load_model = args.load_model
model_dir = args.model_dir

# Environment parameters
n_actions = env.action_space.n
n_states = 84*84

# Hyper parameters
batch_size = 32
lr = 0.01                 # learning rate
epsilon = 0.1           # epsilon-greedy
gamma = 0.9               # reward discount factor
target_replace_iter = 100  # target network 更新間隔
memory_capacity = 5000
n_episodes = 5000
max_step = 1000
save_freq = 100           # save model frequency

env.set_maxsteps(max_step)

# 建立 DQN
dqn = DQN(n_states, n_actions, batch_size, lr, epsilon, gamma,
          target_replace_iter, memory_capacity, use_gpu)

if args.phase == 'train':

    print('----------Training start!----------')

    # load model
    if load_model == True:
        model_list = glob(os.path.join(model_dir, '*.pt'))
        start_iter = 0
        if not len(model_list) == 0:
            model_list.sort()
            start_iter = int(model_list[-1].split('_')[-1].split('.')[0])
            dqn.load(os.path.join(model_dir), start_iter)
            print(" [*] Load SUCCESS (%.5d)" % start_iter)

    # 學習
    for i_episode in range(start_iter, n_episodes):
        t = 0
        rewards = 0
        state = env.reset()
        start_time = time.time()

        while True:
            env.render()

            # 選擇action
            action = dqn.choose_action(state)
            next_state, reward, done, info = env.step(action)

            # 儲存 experience
            dqn.store_transition(state, action, reward, next_state)

            # 累積reward
            rewards += reward

            # 有足夠experience後進行訓練
            if dqn.memory_counter > memory_capacity:
                dqn.learn()

            # 進入下一state
            state = next_state

            if done:
                print('Episode {} finished after {} time steps, total rewards {}. Current time = {:.2f}s'.format(
                    i_episode, t+1, rewards, time.time() - start_time))

                fp = open("reward.txt", "a")
                fp.write('Episode {}: {} time steps, total rewards {:.2f}\n'.format(
                    i_episode, t+1, rewards))
                fp.close()

                dqn.writer.add_scalar('rewards', rewards, i_episode)

                break

            t += 1

        if i_episode % save_freq == 0:
            dqn.save(os.path.join(model_dir), i_episode)
            print(" [*] Save SUCCESS (%.5d)" % i_episode)

    env.close()

if args.phase == 'test':

    print('----------Testing start!----------')

    # load model
    model_list = glob(os.path.join(model_dir, '*.pt'))
    start_iter = 0
    if not len(model_list) == 0:
        model_list.sort()
        start_iter = int(model_list[-1].split('_')[-1].split('.')[0])
        dqn.load(os.path.join(model_dir), start_iter)
        print(" [*] Load SUCCESS (%.5d)" % start_iter)

    # test
    success = 0         # The number of success games
    test_games = 100    # The number of test games

    for i_episode in range(test_games):
        t = 0
        rewards = 0
        state = env.reset()
        start_time = time.time()

        while True:
            env.render()

            # 選擇action
            action = dqn.choose_action_test(state)
            next_state, reward, done, info = env.step(action)

            # 儲存 experience
            dqn.store_transition(state, action, reward, next_state)

            # 累積reward
            rewards += reward

            # 進入下一state
            state = next_state
            
            if done or reward > 0:
                print('reward = ', reward)
                if reward > 0:
                    success += 1
                
                print('Episode {} finished after {} time steps, success ratio = {}/{}. Current time = {:.2f}s'.format(
                    i_episode, t+1, success, test_games, time.time() - start_time))

                fp = open("test.txt", "a")
                fp.write('Episode {} finished after {} time steps, success ratio = {}/{}. Current time = {:.2f}s\n'.format(
                    i_episode, t+1, success, test_games, time.time() - start_time))
                fp.close()

                break

            t += 1

    env.close()
