import gym
import torch
import torch.nn as nn
import torch.optim as optim
import random
import numpy as np
import matplotlib.pyplot as plt
from tqdm import tqdm  # 引入进度条
from collections import deque


import sys
sys.path.append('/home/shengjiaao/Newpython/C_2024')
import warnings
warnings.filterwarnings("ignore")  # 忽略所有警告
from env_new import CropPlantingEnv
from models import EnhancedDuelingCropQNetwork

import numpy as np

class SumTree:
    def __init__(self, capacity):
        self.capacity = capacity  # 容量
        self.tree = np.zeros(2 * capacity - 1)  # 树结构，用于存储优先级
        self.data = np.zeros(capacity, dtype=object)  # 存储经验数据
        self.write = 0  # 数据写入的位置
        self.n_entries = 0  # 经验条目数量

    def _propagate(self, idx, change):
        """向上传递优先级变化"""
        parent = (idx - 1) // 2
        self.tree[parent] += change
        if parent != 0:
            self._propagate(parent, change)

    def update(self, idx, p):
        """更新某个位置的优先级"""
        change = p - self.tree[idx]
        self.tree[idx] = p
        self._propagate(idx, change)

    def add(self, p, data):
        """添加新的经验和优先级"""
        idx = self.write + self.capacity - 1
        self.data[self.write] = data
        self.update(idx, p)

        self.write += 1
        if self.write >= self.capacity:  # 当容量达到最大值时，覆盖旧的经验
            self.write = 0

        if self.n_entries < self.capacity:
            self.n_entries += 1

    def _retrieve(self, idx, s):
        """根据优先级进行采样"""
        left = 2 * idx + 1
        right = left + 1

        if left >= len(self.tree):  # 如果到达叶子节点
            return idx

        if s <= self.tree[left]:
            return self._retrieve(left, s)
        else:
            return self._retrieve(right, s - self.tree[left])

    def get(self, s):
        """根据采样值s，获取优先级和经验"""
        idx = self._retrieve(0, s)
        data_idx = idx - self.capacity + 1
        return (idx, self.tree[idx], self.data[data_idx])

    @property
    def total(self):
        return self.tree[0]  # 总优先级


# DQN Agent
class DQNAgent:
    def __init__(self, input_shape, output_shape, use_enhanced=True, device=None):
        self.input_shape = input_shape
        self.output_shape = output_shape
        self.memory = SumTree(100000)  # 使用 SumTree 实现的优先经验回放
        self.alpha = 0.6  # 优先级采样的权重
        self.beta = 0.4  # 重要性采样的权重
        self.beta_increment_per_sampling = 0.001
        self.epsilon_priority = 0.01  # 防止优先级为 0
        self.abs_error_upper = 1.0  # TD误差的上界

        self.gamma = 0.99  # 折扣因子
        self.epsilon = 1.0  # 探索率
        self.epsilon_min = 0.01
        self.epsilon_decay = 0.9995
        self.learning_rate = 0.0001
        self.batch_size = 512
        self.target_update_freq = 5
        self.device = torch.device('cuda')

        self.model = EnhancedDuelingCropQNetwork(input_shape, output_shape).to(self.device)
        self.target_model = EnhancedDuelingCropQNetwork(input_shape, output_shape).to(self.device)

        if torch.cuda.device_count() > 1:
            self.model = nn.DataParallel(self.model)
            self.target_model = nn.DataParallel(self.target_model)

        self.update_target_model()  # 初始化目标模型
        self.optimizer = optim.Adam(self.model.parameters(), lr=self.learning_rate)
        self.criterion = nn.MSELoss()

    def update_target_model(self):
        """更新目标网络的权重"""
        self.target_model.load_state_dict(self.model.state_dict())

    def act(self, state):
        """根据当前状态选择动作"""
        if np.random.rand() <= self.epsilon:
            # 选择随机动作
            return np.random.uniform(0, 1, self.output_shape)  # 返回符合动作空间的随机动作

        # 加入 batch 维度，确保输入符合神经网络的批量处理要求
        state = torch.FloatTensor(state).unsqueeze(0).to(self.device)

        # 获取 Q 值输出
        act_values = self.model(state)

        # 去掉 batch 维度，返回与动作空间匹配的动作
        return act_values.cpu().detach().numpy().squeeze(0)

    def remember(self, state, action, reward, next_state, done):
        """将经验存入 SumTree，并分配初始优先级"""
        max_priority = np.max(self.memory.tree[-self.memory.capacity:])
        if max_priority == 0:
            max_priority = self.abs_error_upper
        self.memory.add(max_priority, (state, action, reward, next_state, done))

    def replay(self):
        if self.memory.n_entries < self.batch_size:
            return

        # 从 SumTree 中采样经验
        minibatch = []
        idxs = []
        segment = self.memory.total / self.batch_size
        priorities = []
        for i in range(self.batch_size):
            s = random.uniform(i * segment, (i + 1) * segment)
            (idx, p, data) = self.memory.get(s)
            minibatch.append(data)
            idxs.append(idx)
            priorities.append(p)

        # 转换为训练所需的格式
        states, targets_f = [], []
        for state, action, reward, next_state, done in minibatch:
            state = torch.FloatTensor(state).unsqueeze(0).to(self.device)
            next_state = torch.FloatTensor(next_state).unsqueeze(0).to(self.device)

            target = reward
            if not done:
                with torch.no_grad():
                    next_action = torch.argmax(self.model(next_state), dim=1)
                    target_value = self.target_model(next_state)[0, next_action].item()
                    target = reward + self.gamma * target_value

            target_f = self.model(state).clone()
            target_f[0] = torch.FloatTensor(action).to(self.device)
            target_f[0] = target

            states.append(state)
            targets_f.append(target_f)

        states = torch.cat(states)
        targets_f = torch.cat(targets_f)

        self.optimizer.zero_grad()
        loss = self.criterion(self.model(states), targets_f)
        loss.backward()
        self.optimizer.step()

        # 更新每个采样经验的优先级
        for i in range(self.batch_size):
            idx = idxs[i]
            priority = abs(priorities[i]) + self.epsilon_priority
            self.memory.update(idx, priority)

        if self.epsilon > self.epsilon_min:
            self.epsilon *= self.epsilon_decay

        return loss.item()

    def load(self, name):
        """加载训练好的模型"""
        self.model.load_state_dict(torch.load(name))

    def save(self, name):
        """保存训练好的模型"""
        torch.save(self.model.state_dict(), name)


# 主训练过程
if __name__ == "__main__":

    env = CropPlantingEnv()
    observation_shape = env.observation_space.shape
    action_shape = env.action_space.shape
    print("action_shape", action_shape)
    agent = DQNAgent(observation_shape, action_shape)

    # 训练参数
    episodes = 1000
    done = False
    rewards = []
    losses = []
    best_reward = -float('inf')
    save_model_path = '/home/ziwu/Newpython/C_2024/algo/Q2/DuelingDQN/DuelingDQN_attention/checkpoints/dqn_atten_.pth'

    # 使用 tqdm 显示进度条
    for e in tqdm(range(episodes), desc="Training Progress"):
        state = env.reset()
        episode_reward = 0
        episode_loss = 0

        for time in range(1000):
            action = agent.act(state)  # 动作是输出的 3D 矩阵
            # print("action.shape", action.shape)
            next_state, reward, done, _ = env.step(action)
            episode_reward += reward
            agent.remember(state, action, reward, next_state, done)
            state = next_state

            # 训练并获取 loss
            loss = agent.replay()
            if loss is not None:
                episode_loss += loss


            if done:
                break

        # 更新 target 网络
        if e % agent.target_update_freq == 0:
            agent.update_target_model()

        # 保存训练奖励和 loss
        rewards.append(episode_reward)
        losses.append(episode_loss)

        # 打印每10轮的训练结果
        if e % 10 == 0:
            print(
                f"Episode: {e}/{episodes}, Reward: {episode_reward:.2f}, Loss: {episode_loss:.4f}, Epsilon: {agent.epsilon:.2f}")

        # 保存最优模型
        if episode_reward > best_reward:
            best_reward = episode_reward
            agent.save(save_model_path)
            print(f"Best model saved with reward: {best_reward:.2f}")

    # 绘制训练奖励和 loss 曲线
    plt.figure(figsize=(12, 6))

    # 绘制奖励曲线
    plt.subplot(1, 2, 1)
    plt.plot(rewards)
    plt.title('Training Rewards')
    plt.xlabel('Episode')
    plt.ylabel('Reward')

    # 绘制 loss 曲线
    plt.subplot(1, 2, 2)
    plt.plot(losses)
    plt.title('Training Loss')
    plt.xlabel('Episode')
    plt.ylabel('Loss')

    plt.tight_layout()
    plt.show()
