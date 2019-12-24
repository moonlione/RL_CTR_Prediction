import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
import random

def setup_seed(seed):
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    np.random.seed(seed)
    random.seed(seed)
    torch.backends.cudnn.deterministic = True
# 设置随机数种子
setup_seed(1)

neural_nums_a_1 = 100
neural_nums_a_2 = 100
neural_nums_c_1 = 100
neural_nums_c_2 = 100

class Fature_embedding(nn.Module):
    def __init__(self, feature_numbers, field_nums, latent_dims):
        super(Fature_embedding, self).__init__()
        self.field_nums = field_nums
        self.latent_dims = latent_dims

        pretrain_params = torch.load('models/model_params/FFMbest.pth')

        self.field_feature_embeddings = nn.ModuleList([
            nn.Embedding(feature_numbers, latent_dims) for _ in range(field_nums)
        ])  # 相当于建立一个field_nums * feature_nums * latent_dims的三维矩阵
        for i, embedding in enumerate(self.field_feature_embeddings):
            self.field_feature_embeddings[i].weight.data.copy_(
                torch.from_numpy(
                    np.array(pretrain_params['field_feature_embeddings.' + str(i) + '.weight'].cpu())
                )
            )

        self.linear = nn.Embedding(feature_numbers, 1)
        self.linear.weight.data.copy_(
            torch.from_numpy(
                np.array(pretrain_params['linear.weight'].cpu())
            )
        )

    def forward(self, x):
        x_second_embedding = [self.field_feature_embeddings[i](x) for i in range(self.field_nums)]
        embedding_vectors = torch.FloatTensor().cuda()
        for i in range(self.field_nums - 1):
            for j in range(i + 1, self.field_nums):
                embedding_vectors = torch.cat([embedding_vectors, (x_second_embedding[j][:, i] * x_second_embedding[i][:, j])], dim=1)

        for i, embedding in enumerate(self.field_feature_embeddings):
            embedding_vectors = torch.cat([embedding_vectors, embedding(x[:, i])], dim=1)

        x_linear_embedding = self.linear(x).view(-1, self.field_nums)

        embedding_vectors = torch.cat([embedding_vectors, x_linear_embedding], dim=1)

        return embedding_vectors
        # return self.field_feature_embeddings(x).view(-1, self.field_nums * self.latent_dims) # m * n矩阵平铺为1 * [m*n]

class Actor(nn.Module):
    def __init__(self, input_dims, action_numbers):
        super(Actor, self).__init__()
        self.fc1 = nn.Linear(input_dims, neural_nums_a_1)
        self.fc2 = nn.Linear(neural_nums_a_1, neural_nums_a_2)
        self.out = nn.Linear(neural_nums_a_2, action_numbers)

    def forward(self, input):
        x = F.relu(self.fc1(input))
        x_ = F.relu(self.fc2(x))
        out = torch.sigmoid(self.out(x_))

        return out

class Critic(nn.Module):
    def __init__(self, input_dims, action_numbers):
        super(Critic, self).__init__()
        self.fc_s = nn.Linear(input_dims, neural_nums_c_1)
        self.fc_a = nn.Linear(action_numbers, 1)
        self.fc_q = nn.Linear(1 + neural_nums_c_1, neural_nums_c_2)
        self.fc_ = nn.Linear(neural_nums_c_2, 1)

    def forward(self, input, action):
        f_s = F.relu(self.fc_s(input))
        f_a = self.fc_a(action)
        cat = torch.cat([f_s, f_a], dim=1)
        q = F.relu(self.fc_q(cat))
        q = self.fc_(q)

        return q

class DDPG():
    def __init__(
            self,
            feature_nums,
            field_nums = 15,
            action_nums = 1,
            latent_dims=5,
            lr_A = 1e-4,
            lr_C = 1e-3,
            reward_decay = 1,
            memory_size = 1000000,
            batch_size = 256,
            tau = 0.001, # for target network soft update
            device = 'cuda:0',
    ):
        self.feature_nums = feature_nums
        self.field_nums = field_nums
        self.action_nums = action_nums
        self.lr_A = lr_A
        self.lr_C = lr_C
        self.gamma = reward_decay
        self.latent_dims = latent_dims
        self.memory_size = memory_size
        self.batch_size = batch_size
        self.tau = tau
        self.device = device

        self.memory_counter = 0

        input_dims = 0
        for i in range(self.field_nums - 1):
            for j in range(i + 1, self.field_nums):
                input_dims += 5
        input_dims += 90 # 15+75
        self.input_dims = input_dims

        self.buffer = list()
        self.memory = np.zeros((self.memory_size, self.input_dims * 2 + self.action_nums + 1))

        self.embedding_layer = Fature_embedding(self.feature_nums, self.field_nums, self.latent_dims).to(self.device)

        self.Actor = Actor(self.input_dims, self.action_nums).to(self.device)
        self.Critic = Critic(self.input_dims, self.action_nums).to(self.device)

        self.Actor_ = Actor(self.input_dims, self.action_nums).to(self.device)
        self.Critic_ = Critic(self.input_dims, self.action_nums).to(self.device)

        # 优化器
        self.optimizer_a = torch.optim.Adam(self.Actor.parameters(), lr=self.lr_A)
        self.optimizer_c = torch.optim.Adam(self.Critic.parameters(), lr=self.lr_C, weight_decay=1e-3)

        self.loss_func = nn.MSELoss(reduction='mean')

    def store_transition(self, transition):
        self.buffer.append(transition)
        print(torch.from_numpy(np.array(self.buffer)))

        buffer_indexs = [0, torch.FloatTensor(self.buffer)]
        self.memory
        print(self.memory)
        # transition = transition.cpu().detach().numpy()
        # # 由于已经定义了经验池的memory_size，如果超过此大小，旧的memory则被新的memory替换
        # index_start = self.memory_counter % self.memory_size
        # index_end = (self.memory_counter + len(transition)) % self.memory_size
        #
        # if index_end > index_start:
        #     self.memory[index_start: index_end, :] = transition  # 替换
        # else:
        #     replace_len_1 = self.memory_size - index_start
        #     self.memory[index_start: self.memory_size, :] = transition[0: replace_len_1]
        #     replace_len_2 = len(transition) - replace_len_1
        #     self.memory[0: replace_len_2, :] = transition[replace_len_1: len(transition)]
        #
        # print(np.where(self.memory == np.zeros((1, self.input_dims * 2 + self.action_nums + 1))))
        # self.memory_counter = 2048
        #
        # print(self.memory_counter)

    def choose_action(self, state):
        state = self.embedding_layer.forward(state)

        self.Actor.eval()
        with torch.no_grad():
            action = self.Actor.forward(state).cpu().numpy()
        self.Actor.train()

        return state, action

    def soft_update(self, net, net_target):
        for param_target, param in zip(net_target.parameters(), net.parameters()):
            param_target.data.copy_(param_target.data * (1.0 - self.tau) + param.data * self.tau)

    def learn(self):
        if self.memory_counter > self.memory_size:
            # replacement 代表的意思是抽样之后还放不放回去，如果是False的话，那么出来的三个数都不一样，如果是True的话， 有可能会出现重复的，因为前面的抽的放回去了
            sample_index = np.random.choice(self.memory_size, size=self.batch_size, replace=False)
        else:
            sample_index = np.random.choice(self.memory_counter, size=self.batch_size)

        batch_memory = self.memory[sample_index, :]

        b_s = torch.FloatTensor(batch_memory[:, :self.input_dims]).to(self.device)
        b_a = torch.FloatTensor(batch_memory[:, self.input_dims: self.input_dims + self.action_nums]).to(self.device)
        b_r = torch.FloatTensor(batch_memory[:, -self.input_dims - 1: -self.input_dims]).to(self.device)
        b_s_ = torch.FloatTensor(batch_memory[:, -self.input_dims:]).to(self.device)

        q_target = b_r + self.gamma * self.Critic_.forward(b_s_, self.Actor_.forward(b_s_))
        q = self.Critic.forward(b_s, b_a)
        td_error = F.smooth_l1_loss(q, q_target.detach())
        self.optimizer_c.zero_grad()
        td_error.backward()
        self.optimizer_c.step()

        a_loss = -self.Critic.forward(b_s, self.Actor.forward(b_s)).mean()

        self.optimizer_a.zero_grad()
        a_loss.backward()
        self.optimizer_a.step()

        td_error_r = td_error.item()
        a_loss_r = a_loss.item()
        return td_error_r, a_loss_r

class OrnsteinUhlenbeckNoise:
    def __init__(self, mu):
        self.theta, self.dt, self.sigma = 0.15, 0.01, 0.2
        self.mu = mu
        self.x_prev = np.zeros_like(self.mu)

    def __call__(self):
        x = self.x_prev + self.theta * (self.mu - self.x_prev) * self.dt + \
                self.sigma * np.sqrt(self.dt) * np.random.normal(size=self.mu.shape)
        self.x_prev = x
        return x
