# Purpose: to simplify our intermediate work into just the final product that we have so far

import torch
import torch.nn as nn
from torch.nn import functional as F

# hyperparameters
batch_size = 32
block_size = 8
max_iters = 5000
eval_interval = 300
learning_rate = 1e-3 # self-attention can't tolerate high learning rates like 1e-2 (it will diverge)
device = 'cuda' if torch.cuda.is_available() else 'cpu'
eval_iters = 200
n_embd = 32
# ------------

torch.manual_seed(1337) # for reproducibility

# Load data
# !wget https://raw.githubusercontent.com/karpathy/char-rnn/master/data/tinyshakespeare/input.txt
with open('input.txt', 'r', encoding='utf-8') as f:
    text = f.read()

# Create encoder and decoder
# all the unique chars that occur in the text
chars = sorted(list(set(text)))
vocab_size = len(chars)
# create a mapping from characters to integers (encoder, decoder)
stoi = { ch:i for i,ch in enumerate(chars) }
itos = { i:ch for i,ch in enumerate(chars) }
encode = lambda s: [stoi[c] for c in s]
decode = lambda l: ''.join([itos[i] for i in l]) 

# Train and test splits
data = torch.tensor(encode(text), dtype=torch.long)
n = int(0.9*len(data)) # first 90% will be train, rest val
train_data = data[:n]
val_data = data[n:]

# data loading
def get_batch(split):
    # generate a small batch of data of inputs x and targets y
    data = train_data if split == 'train' else val_data
    ix = torch.randint(len(data) - block_size, (batch_size,))
    x = torch.stack([data[i:i+block_size] for i in ix]) 
    y = torch.stack([data[i+1:i+block_size+1] for i in ix]) 
    x, y = x.to(device), y.to(device) # load data to device
    return x, y

@torch.no_grad() # do not call .backward, more efficient because it doesn't need to store gradients, much more memory efficient
def estimate_loss():
    out = {}
    model.eval() # set model to evaluation phase
    for split in ['train', 'val']:
        losses = torch.zeros(eval_iters)
        for k in range(eval_iters):
            X, Y = get_batch(split)
            logits, loss = model(X, Y)
            losses[k] = loss.item()
        out[split] = losses.mean()
    model.train() # set model back to training phase (useful if there's dropout or batch norm that you want only in training but not inference / eval)
    return out

class Head(nn.Module):
  ''' one head of self-attention '''

  def __init__(self, head_size):
      super().__init__()
      self.key = nn.Linear(n_embd, head_size, bias=False)
      self.query = nn.Linear(n_embd, head_size, bias=False)
      self.value = nn.Linear(n_embd, head_size, bias=False)
      self.register_buffer('tril', torch.tril(torch.ones(block_size, block_size))) # tril (lower triangular matrix) variable, a buffer (not parameter!) that you don't want to update, but are part of the module / model's state and saved when using torch.save

  def forward(self, x):
      B,T,C = x.shape
      k = self.key(x) # (B,T,C)
      q = self.query(x) # (B,T,C)
      # compute attention scores
      wei = q @ k.transpose(-2, -1) * k.size(-1)**-0.5
      wei = wei.masked_fill(self.tril[:T, :T] == 0, float('-inf')) # (B, T, T), slicing the lower triangular matrix in case T < block_size
      wei = F.softmax(wei, dim=-1) # (B, T, T)
      # perform the weighted aggregation of the values
      v = self.value(x) # (B,T,head_size)
      out = wei @ v # (B, T, T) @ (B, T, head_size) = (B, T, head_size)
      return out

class MultiHeadAttention(nn.Module):
  ''' multiple heads of self-attention in parallel '''

  def __init__(self, num_heads, head_size):
      super().__init__()
      self.heads = nn.ModuleList((Head(head_size) for _ in range(num_heads))) # (B, T, head_size)
      self.proj = nn.Linear(n_embd, n_embd) # (B, T, n_embd)  # projection to help with feature transformation for a better representation space

  def forward(self, x):
      out = torch.cat([h(x) for h in self.heads], dim=-1) # (B, T, head_size * num_heads) # problem: PyTorch helps run in parallel on the GPU level if you're on a GPU and is parallelized across the multiple cores of the GPU. Tensor operations like dot products or element-wise operations are inherently parallelized on the GPU. No need to maange parallelism explicitly through multithreading or multiprocessing in Python, yay. To leverage, just move to GPU with .to(device) and PyTorch handles the rest, executing model operations in parallel when possible.
      out = self.proj(out) # (B, T, n_embd)  # projection to help with feature transformation for a better representation space
      return out

class FeedForward(nn.Module):
  ''' a simple linear layer followed by a non-linearity '''

  def __init__(self, n_embd):
      super().__init__()
      self.net = nn.Sequential(
         nn.Linear(n_embd, 4 * n_embd),
         nn.ReLU(), # just applies ReLU element-wise
         nn.Linear(4 * n_embd, n_embd), # projection to help with feature transformation for a better representation space
      ) # Note: this is happening on a per-token basis

  def forward(self, x):
      # x is (B, T, n_embd)
      return self.net(x) # (B, T, n_embd) 

class Block(nn.Module):
  ''' Transformer block: attention followed by feedforward '''

  def __init__(self, n_emd, n_head):
      # n_embd: embedding dimension, n_head: the number of heads we'd like
      super().__init__()
      head_size = n_embd // n_head
      self.sa = MultiHeadAttention(n_head, head_size)
      self.ffwd = FeedForward(n_embd)
      self.ln1 = nn.LayerNorm(n_embd)
      self.ln2 = nn.LayerNorm(n_embd)

  def forward(self, x):
      x = x + self.sa(self.ln1(x)) # pre-norm with layer norm across our embedding vector (batch and time are batch dimensions, and so it's like per-token normalization)
      x = x + self.ffwd(self.ln2(x))
      return x

# super simple bigram model
class BigramLanguageModel(nn.Module):

  def __init__(self):
    super().__init__()
    # each token directly reads off the logits for the next token from a lookup table
    self.token_embedding_table = nn.Embedding(vocab_size, n_embd) # NOTE: LOOKUP TABLE, NOT NEURAL NETWORK
    self.position_embedding_table = nn.Embedding(block_size, n_embd) # NOTE: LOOKUP TABLE. need positional encodings which is a learned embedding table for each index and the embedding of that index
    self.blocks = nn.Sequential(
       Block(n_embd, n_head=4),
       Block(n_embd, n_head=4),
       Block(n_embd, n_head=4),
       nn.LayerNorm(n_embd), # normally, there's a final layernorm before the final linear layer that predicts logits
    )
    # self.sa_heads = MultiHeadAttention(4, n_embd//4) # self-attention head so the output is still (B, T, n_embd) from running 4 heads of 8-dim self attention. Kind of like grouped convolution
    # self.ffwd = FeedForward(n_embd)
    self.lm_head = nn.Linear(n_embd, vocab_size) # go from token embeddings to logits, we need a linear layer # from the n_embed to vocab_size. EFfectively, it's just a weight matrix of size (vocab_size, n_embed) and a bias vector of size (vocab_size, 1)

  def forward(self, idx, targets=None):
    B, T = idx.shape

    # idx and targets are both (B,T) tensor of integers
    tok_emb = self.token_embedding_table(idx) # (B,T,n_embd) 
    pos_emb = self.position_embedding_table(torch.arange(T, device=device)) # (T,n_embd), it's an array from range 0 to T-1, with step = 1 and tensors put on the device
    x = tok_emb + pos_emb # (B,T,n_embd) # NOTE: broadcasting works here because pos_emb gets right aligned, the batch dimension gets added, and then it just gets added across the batch dimension of tok_emb
    x = self.blocks(x) # apply one block of self-attention and feedforward (B, T, n_embd)
    logits = self.lm_head(x) # (B,T,vocab_size). x = token + pos embedding

    if targets is None:
      loss = None
    else: # evaluate loss here
      B, T, C = logits.shape
      logits = logits.view(B*T, C)
      targets = targets.view(B*T) 
      loss = F.cross_entropy(logits, targets) 

    return logits, loss

  def generate(self, idx, max_new_tokens):
    # idx is (B, T) array of indices in the current context
    for _ in range(max_new_tokens):
      # crop idx to the last block_size tokens
      idx_cond = idx[:, -block_size:] # take the last block_size time steps from each batch
      # get the predictions
      logits, loss = self(idx_cond)
      # focus only on the last time step
      logits = logits[:, -1, :] # becomes (B, C).
      # apply softmax to get probabilities
      probs = F.softmax(logits, dim=-1) # (B, C)
      # sample from the distribution
      idx_next = torch.multinomial(probs, num_samples=1) # (B, 1)
      # append sampled index to the running sequence
      idx = torch.cat((idx, idx_next), dim=1) # (B, T+1)
    return idx
  
model = BigramLanguageModel()
m = model.to(device) # move model parameters to device

# create a PyTorch optimizer
optimizer = torch.optim.AdamW(m.parameters(), lr=learning_rate)

for iter in range(max_iters):
   
    # every once in a while evaluate the loss on train and val sets
    if iter % eval_interval == 0:
      losses = estimate_loss()
      print(f"step {iter}: train loss {losses['train']:.4f}, val loss {losses['val']:.4f}")

    # sample a batch of data
    xb, yb = get_batch('train')
    
    # evaluate the loss
    logits, loss = model(xb, yb)
    optimizer.zero_grad(set_to_none=True)
    loss.backward()
    optimizer.step()

# generate from the model
context = torch.zeros((1, 1), dtype=torch.long, device=device)
print(decode(m.generate(context, max_new_tokens=500)[0].tolist()))