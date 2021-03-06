import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
import random
from torch.distributions import MultivariateNormal, Categorical
import datetime
from torch.distributions import Normal, Categorical, MultivariateNormal

def setup_seed(seed):
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    np.random.seed(seed)
    random.seed(seed)
    torch.backends.cudnn.deterministic = True


# 设置随机数种子
setup_seed(1)

class SumTree(object):
    def __init__(self, nodes, transition_lens, device): # transition_lens = feature_nums + c_a_nums + d_a_nums + reward_nums
        self.nodes = nodes # leaf nodes
        self.device = device

        self.sum_tree = torch.zeros(size=[2 * self.nodes - 1, 1]).to(self.device) # parents nodes = nodes - 1

        self.data = torch.zeros(size=[self.nodes, transition_lens]).to(self.device)

        self.data_pointer = 0

    def add_leaf(self, p, transitions): # p-priority
        tree_idx_start = self.data_pointer + self.nodes - 1 #python从0开始索引，初始时tree_idx表示第一个叶节点的索引值，样本按叶子结点依次向后排
        tree_idx_end = tree_idx_start + len(transitions)
        self.data[self.data_pointer: self.data_pointer + len(transitions), :] = transitions
        self.update(tree_idx_start, tree_idx_end, p)

        self.data_pointer += len(transitions)

        if self.data_pointer >= self.nodes:
            self.data_pointer = 0

    def update(self, tree_idx_start, tree_idx_end, p):
        changes = p - self.sum_tree[tree_idx_start: tree_idx_end]
        self.sum_tree[tree_idx_start: tree_idx_end] = p

        temp_tree_idxs = np.arange(tree_idx_start, tree_idx_end)
        for i, tree_idx in enumerate(temp_tree_idxs):
            while tree_idx != 0:
                tree_idx = (tree_idx - 1) // 2
                self.sum_tree[tree_idx] += changes[i] # 这里要改，因为parents_tree_idx会有相同的部分

    def batch_update(self, tree_idx, p):
        changes = p - self.sum_tree[tree_idx]
        self.sum_tree[tree_idx] = p

        for i, tree_idx in enumerate(tree_idx):
            while tree_idx != 0:
                tree_idx = (tree_idx - 1) // 2
                self.sum_tree[tree_idx] += changes[i]  # 这里要改，因为parents_tree_idx会有相同的部分

    def get_leaf(self, v): # v-随机选择的p值，用于抽取data， 只有一条
        parent_idx = 0

        while True:
            cl_idx = 2 * parent_idx + 1
            cr_idx = cl_idx + 1

            if cl_idx >= len(self.sum_tree): # 没有子节点了
                leaf_idx = parent_idx
                break
            else:
                if v <= self.sum_tree[cl_idx]:
                    parent_idx = cl_idx
                else:
                    v -= self.sum_tree[cl_idx]
                    parent_idx = cr_idx

        data_idx = leaf_idx - self.nodes + 1 # 减去非叶子节点索引数

        return leaf_idx, self.sum_tree[leaf_idx], self.data[data_idx]

    @property
    def total_p(self):
        return self.sum_tree[0] # root's total priority

class Memory(object):
    def __init__(self, nodes, transition_lens, device):
        self.sum_tree = SumTree(nodes, transition_lens, device)

        self.device = device
        self.nodes = nodes
        self.transition_lens = transition_lens # 存储的数据长度
        self.epsilon = 1e-3 # 防止出现zero priority
        self.alpha = 0.6 # 取值范围(0,1)，表示td error对priority的影响
        self.beta = 0.4 # important sample， 从初始值到1
        self.beta_increment_per_sampling = 0.0001
        self.abs_err_upper = 1 # abs_err_upper和epsilon ，表明p优先级值的范围在[epsilon,abs_err_upper]之间，可以控制也可以不控制

    def get_priority(self, td_error):
        return torch.pow(torch.abs(td_error) + self.epsilon, self.alpha)

    def add(self, td_error, transitions): # td_error是tensor矩阵
        p = self.get_priority(td_error)
        self.sum_tree.add_leaf(p, transitions)

    def sample(self, batch_size):
        batch = torch.zeros(size=[batch_size, self.transition_lens]).to(self.device)
        tree_idx = torch.zeros(size=[batch_size, 1]).to(self.device)
        ISweights = torch.zeros(size=[batch_size, 1]).to(self.device)

        segment = self.sum_tree.total_p / batch_size

        min_prob = torch.min(self.sum_tree.sum_tree[-self.nodes:]) / self.sum_tree.total_p
        self.beta = torch.min(torch.FloatTensor([1., self.beta + self.beta_increment_per_sampling])).item()

        for i in range(batch_size):
            a = segment * i
            b = segment * (i + 1)

            v = random.uniform(a, b)
            idx, p, data = self.sum_tree.get_leaf(v)

            prob = p / self.sum_tree.total_p

            ISweights[i] = torch.pow(torch.div(prob, min_prob), -self.beta)
            batch[i], tree_idx[i] = data, idx

        return tree_idx, batch, ISweights

    def batch_update(self, tree_idx, td_errors):
        p = self.get_priority(td_errors)
        self.sum_tree.batch_update(tree_idx, p)

class Critic(nn.Module):
    def __init__(self, input_dims, c_action_nums, d_action_nums):
        super(Critic, self).__init__()
        self.input_dims = input_dims
        self.c_action_nums = c_action_nums
        self.d_action_nums = d_action_nums

        self.bn_input = nn.BatchNorm1d(self.input_dims)

        deep_input_dims = self.input_dims + self.c_action_nums + self.d_action_nums

        neuron_nums = [300, 300, 300]
        self.mlp = nn.Sequential(
            nn.Linear(deep_input_dims, neuron_nums[0]),
            nn.BatchNorm1d(neuron_nums[0]),
            nn.ReLU(),
            nn.Linear(neuron_nums[0], neuron_nums[1]),
            nn.BatchNorm1d(neuron_nums[1]),
            nn.ReLU(),
            nn.Linear(neuron_nums[1], neuron_nums[2]),
            nn.BatchNorm1d(neuron_nums[2]),
            nn.ReLU(),
            nn.Linear(neuron_nums[2], 1)
        )

    def evaluate(self, input, c_actions, d_actions): # actions 包括连续与非连续
        obs = self.bn_input(input)
        cat = torch.cat([obs, c_actions, d_actions], dim=1)

        q_out = self.mlp(cat)

        return q_out

class hybrid_actors(nn.Module):
    def __init__(self, input_dims, action_nums):
        super(hybrid_actors, self).__init__()
        self.input_dims = input_dims
        self.c_action_dims = action_nums
        self.d_action_dims = action_nums - 1

        self.bn_input = nn.BatchNorm1d(self.input_dims)

        neuron_nums = [300, 300, 300]
        self.mlp = nn.Sequential(
            nn.Linear(self.input_dims, neuron_nums[0]),
            nn.BatchNorm1d(neuron_nums[0]),
            nn.ReLU(),
            nn.Linear(neuron_nums[0], neuron_nums[1]),
            nn.BatchNorm1d(neuron_nums[1]),
            nn.ReLU(),
            nn.Linear(neuron_nums[1], neuron_nums[2]),
            nn.BatchNorm1d(neuron_nums[2]),
            nn.ReLU()
        )# 特征提取层

        self.c_action_layer = nn.Sequential(
            nn.Linear(neuron_nums[2], self.c_action_dims),
            nn.Tanh()
        )
        self.d_action_layer = nn.Sequential(
            nn.Linear(neuron_nums[2], self.d_action_dims),
            nn.Softmax(dim=-1)
        )

        self.c_action_std = nn.Parameter(torch.ones(size=[1]))

        self.std = torch.ones(size=[1, self.c_action_dims]).cuda()
        self.mean = torch.zeros(size=[1, self.c_action_dims]).cuda()

        self.std_d = torch.ones(size=[1, self.d_action_dims]).cuda()
        self.mean_d = torch.zeros(size=[1, self.d_action_dims]).cuda()

    def act(self, input, exploration_rate):
        obs = self.bn_input(input)
        mlp_out = self.mlp(obs)

        c_action_means = self.c_action_layer(mlp_out)
        d_action_q_values = self.d_action_layer(mlp_out)

        c_action_dist = c_action_means + Normal(self.mean, self.std * exploration_rate).sample()
        # print(c_action_dist)
        # print(Normal(c_action_means, self.std).sample())
        # print(c_action_means + Normal(self.mean, self.std).sample())
        c_actions = torch.clamp(c_action_dist, -1, 1)  # 用于返回训练
        ensemble_c_actions = torch.softmax(c_actions, dim=-1)

        d_action = torch.softmax(d_action_q_values + Normal(self.mean_d, self.std_d * exploration_rate).sample(), dim=-1)
        # d_actions = d_action_dist.sample()
        ensemble_d_actions = torch.argsort(-d_action)[:, 0] + 2

        return c_actions, ensemble_c_actions, d_action_q_values, ensemble_d_actions.view(-1, 1)

    def evaluate(self, input):
        obs = self.bn_input(input)
        mlp_out = self.mlp(obs)

        c_actions_means = self.c_action_layer(mlp_out)
        d_actions_q_values = self.d_action_layer(mlp_out)

        c_action_dist = Normal(c_actions_means, F.softplus(self.c_action_std))
        c_action_entropy = c_action_dist.entropy()
        # print(c_action_entropy)
        d_action_dist = Categorical(d_actions_q_values)
        d_action_entropy = d_action_dist.entropy()

        return c_actions_means, d_actions_q_values, c_action_entropy, d_action_entropy


class Hybrid_RL_Model():
    def __init__(
            self,
            feature_nums,
            field_nums=15,
            latent_dims=5,
            action_nums=2,
            campaign_id='1458',
            lr_C_A=1e-3,
            lr_D_A=1e-3,
            lr_C=1e-2,
            reward_decay=1,
            memory_size=4096000,
            batch_size=256,
            tau=0.005,  # for target network soft update
            device='cuda:0',
    ):
        self.feature_nums = feature_nums
        self.field_nums = field_nums
        self.c_action_nums = action_nums
        self.d_action_nums = action_nums - 1
        self.campaign_id = campaign_id
        self.lr_C_A = lr_C_A
        self.lr_D_A = lr_D_A
        self.lr_C = lr_C
        self.gamma = reward_decay
        self.latent_dims = latent_dims
        self.memory_size = memory_size
        self.batch_size = batch_size
        self.tau = tau
        self.device = device

        self.memory_counter = 0

        self.input_dims = self.field_nums * (self.field_nums - 1) // 2 + self.field_nums * self.latent_dims

        self.memory = Memory(self.memory_size, self.field_nums + self.c_action_nums + self.d_action_nums + 2, self.device)

        self.Hybrid_Actor = hybrid_actors(self.input_dims, self.c_action_nums).to(self.device)
        self.Critic = Critic(self.input_dims, self.c_action_nums, self.d_action_nums).to(self.device)

        self.Hybrid_Actor_ = hybrid_actors(self.input_dims, self.c_action_nums).to(self.device)
        self.Critic_ = Critic(self.input_dims, self.c_action_nums,  self.d_action_nums).to(self.device)

        # 优化器
        self.optimizer_a = torch.optim.Adam(self.Hybrid_Actor.parameters(), lr=self.lr_C_A, weight_decay=1e-5)
        self.optimizer_c = torch.optim.Adam(self.Critic.parameters(), lr=self.lr_C, weight_decay=1e-5)

        self.loss_func = nn.MSELoss(reduction='mean')

        self.c_action_std = torch.ones(size=[1, self.c_action_nums]).to(self.device)

    def store_transition(self, transitions, embedding_layer): # 所有的值都应该弄成float
        b_s = embedding_layer.forward(transitions[:, :self.field_nums].long())
        b_s_ = b_s
        b_c_a = transitions[:, self.field_nums: self.field_nums + self.c_action_nums]
        b_d_a = transitions[:, self.field_nums + self.c_action_nums: self.field_nums + self.c_action_nums + self.d_action_nums]
        b_r = torch.unsqueeze(transitions[:, -1], dim=1)

        # current state's action_values
        c_actions_means, d_actions_q_values, c_actions_entropy, d_actions_entropy = self.Hybrid_Actor.evaluate(b_s)

        # critic
        q_target_critic = b_r + self.gamma * self.Critic_.evaluate(b_s_, c_actions_means, d_actions_q_values)
        q_critic = self.Critic.evaluate(b_s, b_c_a, b_d_a)
        td_error_critic = q_target_critic - q_critic

        td_errors = td_error_critic

        self.memory.add(td_errors.detach(), transitions)

    def choose_action(self, state, exploration_rate):
        self.Hybrid_Actor.eval()
        with torch.no_grad():
            c_actions, ensemble_c_actions, d_q_values, ensemble_d_actions = self.Hybrid_Actor.act(state, exploration_rate)

        self.Hybrid_Actor.train()
        return c_actions, ensemble_c_actions, d_q_values, ensemble_d_actions

    def choose_best_action(self, state):
        self.Hybrid_Actor.eval()
        with torch.no_grad():
            c_action_means, d_q_values, c_entropy, d_entropy = self.Hybrid_Actor.evaluate(state)

        ensemble_c_actions = torch.softmax(c_action_means, dim=-1)
        ensemble_d_actions = torch.argsort(-d_q_values)[:, 0] + 2

        return ensemble_d_actions.view(-1, 1), ensemble_c_actions

    def soft_update(self, net, net_target):
        for param_target, param in zip(net_target.parameters(), net.parameters()):
            param_target.data.copy_(param_target.data * (1.0 - self.tau) + param.data * self.tau)

    def learn(self, embedding_layer):
        # sample
        choose_idx, batch_memory, ISweights = self.memory.sample(self.batch_size)

        b_s = embedding_layer.forward(batch_memory[:, :self.field_nums].long())
        b_c_a = batch_memory[:, self.field_nums: self.field_nums + self.c_action_nums]
        b_d_a = batch_memory[:, self.field_nums + self.c_action_nums: self.field_nums + self.c_action_nums + self.d_action_nums]
        b_discrete_a = torch.unsqueeze(batch_memory[:, self.field_nums + self.c_action_nums] + self.d_action_nums, 1)
        b_r = torch.unsqueeze(batch_memory[:, -1], 1)
        b_s_ = b_s  # embedding_layer.forward(batch_memory_states)

        # Critic
        c_actions_means_for_critic, d_actions_q_values_for_critic, c_actions_entropy_for_critic, d_actions_entropy_for_critic\
            = self.Hybrid_Actor.evaluate(b_s)
        q_target = b_r + self.gamma * self.Critic_.evaluate(b_s_, c_actions_means_for_critic, d_actions_q_values_for_critic)
        q = self.Critic.evaluate(b_s, b_c_a, b_d_a)

        critic_td_error = (q_target - q).detach()
        critic_loss = (ISweights * torch.pow(q - q_target.detach(), 2)).mean()

        self.optimizer_c.zero_grad()
        critic_loss.backward()
        self.optimizer_c.step()

        critic_loss_r = critic_loss.item()

        # current state's action_values
        c_actions_means, d_actions_q_values, c_actions_entropy, d_actions_entropy = self.Hybrid_Actor.evaluate(b_s)
        # next state's action_values
        c_actions_means_, d_actions_q_values_, c_actions_entropy_, d_actions_entropy_ = self.Hybrid_Actor.evaluate(b_s)

        # Hybrid_Actor
        # c a
        c_a_loss = -self.Critic.evaluate(b_s, c_actions_means, d_actions_q_values).mean()
        # print(c_actions_entropy.mean(), d_actions_entropy.mean())
        # d a
        # q_eval = d_actions_q_values.gather(1, b_discrete_a.long() - 2)  # shape (batch,1), gather函数将对应action的Q值提取出来做Bellman公式迭代
        # q_next = d_actions_q_values_
        # q_target = b_r + self.gamma * q_next.max(1)[0].view(-1, 1)  # shape (batch, 1)
        # # d_a_td_error = (q_target - q_eval).detach()
        # # d_a_loss = (ISweights * torch.pow(q_eval - q_target.detach(), 2)).mean()
        # d_a_loss = torch.pow(q_eval - q_target.detach(), 2).mean()

        actor_loss = c_a_loss
        # actor_loss = c_a_loss

        self.optimizer_a.zero_grad()
        actor_loss.backward()
        self.optimizer_a.step()

        actor_loss_r = actor_loss.item()

        new_p = critic_td_error

        self.memory.batch_update(choose_idx.long().squeeze(1), new_p)

        return critic_loss_r, actor_loss_r


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
