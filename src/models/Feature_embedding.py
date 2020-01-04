import torch
import torch.nn as nn

import numpy as np

# class Feature_Embedding(nn.Module):
#     def __init__(self, feature_numbers, field_nums, latent_dims, campaign_id):
#         super(Feature_Embedding, self).__init__()
#         self.field_nums = field_nums
#         self.latent_dims = latent_dims
#         self.campaign_id = campaign_id
#
#         self.field_feature_embeddings = nn.ModuleList([
#             nn.Embedding(feature_numbers, latent_dims) for _ in range(field_nums)
#         ])
#
#     def forward(self, x):
#         x_second_embedding = [self.field_feature_embeddings[i](x) for i in range(self.field_nums)]
#         embedding_vectors = torch.FloatTensor().cuda()
#         for i in range(self.field_nums):
#             for j in range(i + 1, self.field_nums):
#                 hadamard_product = x_second_embedding[j][:, i] * x_second_embedding[i][:, j]
#                 embedding_vectors = torch.cat([embedding_vectors, hadamard_product], dim=1)
#
#         for i, embedding in enumerate(self.field_feature_embeddings):
#             embedding_vectors = torch.cat([embedding_vectors, embedding(x[:, i])], dim=1)
#
#         return embedding_vectors.detach()

#
class Feature_Embedding(nn.Module):
    def __init__(self, feature_numbers, field_nums, latent_dims, output_dim=1):
        super(Feature_Embedding, self).__init__()
        self.field_nums = field_nums
        self.latent_dims = latent_dims

        self.feature_embedding = nn.Embedding(feature_numbers, latent_dims)
        nn.init.xavier_normal_(self.feature_embedding.weight.data)

        self.linear = nn.Embedding(feature_numbers, output_dim)
        nn.init.xavier_normal_(self.linear.weight.data)

        self.row, self.col = list(), list()
        for i in range(self.field_nums - 1):
            for j in range(i + 1, self.field_nums):
                self.row.append(i), self.col.append(j)

    def forward(self, x):
        x_first_embedding = self.linear(x)

        x_second_embedding = self.feature_embedding(x)

        hadamard_product = torch.mul(x_second_embedding[:, self.row], x_second_embedding[:, self.col])
        inner_product = torch.sum(hadamard_product, dim=2)

        # hadamard_product = hadamard_product.view(-1, self.field_nums * self.latent_dims)
        embedding_vectors = torch.cat([inner_product, x_first_embedding.view(-1, self.field_nums)], dim=1)

        return embedding_vectors
