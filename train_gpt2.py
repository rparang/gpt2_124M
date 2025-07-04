import os
import tiktoken
import numpy as np
import math
import time
import inspect
from dataclasses import dataclass
import torch
import torch.nn as nn
from torch.nn import functional as F

# ---------------------------------------------------------------------------------------

class CausalSelfAttention(nn.Module):

	def __init__(self, config):
		super().__init__()
		assert config.n_embd % config.n_head == 0

		self.c_attn = nn.Linear(config.n_embd, 3 * config.n_embd) # key, query, value projections for all heads but in a batch. Saves you from three separate instantiations of nn.Linear
		self.c_proj = nn.Linear(config.n_embd, config.n_embd) # output projection
		self.c_proj.NANOGPT_SCALE_INIT = 1 # set flag so we know on initialization we need to scale down the std for these residual streams

		self.n_head = config.n_head
		self.n_embd = config.n_embd

		self.register_buffer("bias", torch.tril(torch.ones(config.block_size, config.block_size)).view(1, 1, config.block_size, config.block_size))

	def forward(self, x):

		B, T, C = x.size() # batch size, sequence length, embedding dimension (n_embd)

		# Calculate query, key, value for all heads in batch, move head forward in the shape to be a batch dim alongside B
		# nh is "number of heads", hs is "head size", and C is number of channels (nh * hs)
		# e.g. in GPT-2 (124M), n_head=12, hs=64, so nh*hs = 768 channels in the Transformer

		qkv = self.c_attn(x)
		q, k, v = qkv.split(self.n_embd, dim=2)
		k = k.view(B, T, self.n_head, C // self.n_head).transpose(1, 2) # (B, nh, T, hs)
		q = q.view(B, T, self.n_head, C // self.n_head).transpose(1, 2) # (B, nh, T, hs)
		v = v.view(B, T, self.n_head, C // self.n_head).transpose(1, 2) # (B, nh, T, hs)

		# attention materializes the large (T, T) matrix for all queries and keys
		# att = q @ k.transpose(-2, -1) * (1.0 / math.sqrt(k.size(-1))) # --> (B, nh, T, T)
		# att = att.masked_fill(self.bias[:, :, :T, :T] == 0, float('-inf'))
		# att = F.softmax(att, dim=-1)
		# y = att @ v # (B, nh, T, T) x (B, nh, T, hs) --> (B, nh, T, hs)
		y = F.scaled_dot_product_attention(q, k, v, is_causal=True)
		
		y = y.transpose(1, 2).contiguous().view(B, T, C) # re-assemble all head outputs side by side

		# output project
		y = self.c_proj(y)
		return y


class MLP(nn.Module):

	def __init__(self, config):
		super().__init__()
		self.c_fc = nn.Linear(config.n_embd, 4 * config.n_embd) # On naming (eg 'c_fc'), we are replicating the GPT2 model
		self.gelu = nn.GELU(approximate='tanh')
		self.c_proj = nn.Linear(4 * config.n_embd, config.n_embd)
		self.c_proj.NANOGPT_SCALE_INIT = 1 # set flag so we know on initialization we need to scale down the std for these residual streams

	def forward(self, x):
		x = self.c_fc(x)
		x = self.gelu(x)
		x = self.c_proj(x)
		return x


class Block(nn.Module):

	def __init__(self, config):
		super().__init__()
		self.ln_1 = nn.LayerNorm(config.n_embd)
		self.attn = CausalSelfAttention(config)
		self.ln_2 = nn.LayerNorm(config.n_embd)
		self.mlp = MLP(config)		

	def forward(self, x):
		x = x + self.attn(self.ln_1(x))
		x = x + self.mlp(self.ln_2(x)) 
		return x



@dataclass
class GPTConfig:
	block_size: int = 1024 # max sequence length
	vocab_size: int = 50257 # number of tokens: 50,000 BPE merges + 256 bytes tokens + 1 <|endoftext|> token
	n_layer: int = 12 # number of layers
	n_head: int = 12 # number of heads
	n_embd: int = 768 # embedding dimension

class GPT(nn.Module):
	
	def __init__(self, config):
		super().__init__()
		self.config = config

		self.transformer = nn.ModuleDict(dict(
			wte = nn.Embedding(config.vocab_size, config.n_embd),
			wpe = nn.Embedding(config.block_size, config.n_embd),
			h = nn.ModuleList([Block(config) for _ in range(config.n_layer)]),
			ln_f = nn.LayerNorm(config.n_embd)
		))
		self.lm_head = nn.Linear(config.n_embd, config.vocab_size, bias=False)

		# weight sharing scheme
		self.transformer.wte.weight = self.lm_head.weight

		self.apply(self._init_weights)

	def _init_weights(self, module):
		if isinstance(module, nn.Linear):
			std = 0.02
			if hasattr(module, 'NANOGPT_SCALE_INIT'):
				std *= (2 * self.config.n_layer) ** -0.5 # Scale down the residual streams so std doesn't bloat as the streams add. Note we multiply by 2 bc it happens twice in each Block (one residual in attention, one in MLP)
			torch.nn.init.normal_(module.weight, mean=0.0, std=std)
			if module.bias is not None:
				torch.nn.init.zeros_(module.bias)
		elif isinstance(module, nn.Embedding):
			torch.nn.init.normal_(module.weight, mean=0.0, std=0.02)


	def forward(self, idx, targets=None):
		# idx is shape (B, T)
		B, T = idx.size()
		assert T <= self.config.block_size, f"Cannot forward sequence of length {T}. Block size is only {self.config.block_size}"
		
		# forward the token and position embeddings
		pos = torch.arange(0, T, dtype=torch.long, device=idx.device) # shape (T)
		pos_emb = self.transformer.wpe(pos) # shape (T, n_embd)
		tok_emb = self.transformer.wte(idx) # shape (B, T, n_embd)
		x = tok_emb + pos_emb
		
		# forward through the blocks of the transformer
		for block in self.transformer.h:
			x = block(x)
		
		# forward the final layernorm and the classifier
		x = self.transformer.ln_f(x)
		logits = self.lm_head(x) # (B, T, vocab_size)

		loss = None
		if targets is not None:
			loss = F.cross_entropy(logits.view(-1, logits.size(-1)), targets.view(-1))
		return logits, loss

	def configure_optimizers(self, weight_decay, learning_rate, device):
		# start with all of the candidate parameters (that require grad)
		param_dict = {pn: p for pn, p in self.named_parameters()}
		param_dict = {pn: p for pn, p in param_dict.items() if p.requires_grad}
		# create optim groups. Any parameters that is 2D will be wright decayed, otherwise no
		# i.e. all weight tensors in matmuls + embeddings decay, all biases and laynorms don't
		decay_params = [p for n, p in param_dict.items() if p.dim() >= 2]
		nodecay_params = [p for n, p in param_dict.items() if p.dim() < 2]
		optim_groups = [
			{'params': decay_params, 'weight_decay': weight_decay},
			{'params': nodecay_params, 'weight_decay': 0.0}
		]
		num_decay_params = sum(p.numel() for p in decay_params)
		num_nodecay_params = sum(p.numel() for p in nodecay_params)
		if master_process:
			print(f"Number of decayed parameter tensors: {len(decay_params)}, with {num_decay_params:,} parameters")
			print(f"Number of non-decayed parameter tensors: {len(nodecay_params)}, with {num_nodecay_params:,} parameters")
		# Create the AdamW optimizer and use the fused version if it is available
		fused_available = 'fused' in inspect.signature(torch.optim.AdamW).parameters
		use_fused = fused_available and 'cuda' in device
		if master_process:
			print(f"using fused AdamW: {use_fused}")
		optimizer = torch.optim.AdamW(optim_groups, lr=learning_rate, betas=(0.9, 0.95), eps=1e-8, fused=use_fused)
		return optimizer


	@classmethod
	def from_pretrained(cls, model_type):
		"""Loads pretrained GPT-2 model weights from huggingface"""
		assert model_type in {'gpt2', 'gpt2-medium', 'gpt2-large', 'gpt2-xl'}
		from transformers import GPT2LMHeadModel
		print("loading weights from pretrained gpt: %s" % model_type)

		# n_layer, n_head and n_embd are determined from model_type
		config_args = {
			'gpt2':			dict(n_layer=12, n_head=12, n_embd=768), 	# 124M params
			'gpt2-medium':	dict(n_layer=24, n_head=16, n_embd=1024), 	# 350M params
			'gpt2-large':	dict(n_layer=36, n_head=20, n_embd=1280), 	# 774M param
			'gpt2-xl':		dict(n_layer=48, n_head=25, n_embd=1600), 	# 1558M params
		}[model_type]
		config_args['vocab_size'] = 50257 # always 50257 for GPT model checkpoints
		config_args['block_size'] = 1024  # always 1024 for GPT model checkpoints

		# create a from-scratch initialized minGPT model
		config = GPTConfig(**config_args)
		model = GPT(config)
		sd = model.state_dict()
		sd_keys = sd.keys()
		sd_keys = [k for k in sd_keys if not k.endswith('.attn.bias')] # dicard the mask / buffer

		# init a huggingface/transformers model
		model_hf = GPT2LMHeadModel.from_pretrained(model_type)
		sd_hf = model_hf.state_dict()

		# copy while ensuring all of the parameters are aligned and match in names and shapes
		sd_keys_hf = sd_hf.keys()
		sd_keys_hf = [k for k in sd_keys_hf if not k.endswith('.attn.masked_bias')] # dicard the mask / buffer
		sd_keys_hf = [k for k in sd_keys_hf if not k.endswith('.attn.bias')] # dicard the mask / buffer
		transposed = ['attn.c_attn.weight', 'attn.c_proj.weight', 'mlp.c_fc.weight', 'mlp.c_proj.weight']
		# basically the openai checkppoints use a "Conv1D" module, but we only want to use a vanilla Linear
		# this means we have to transpose these weights when we import them
		assert len(sd_keys_hf) == len(sd_keys), f"mismatched keys: {len(sd_keys_hf)} != {len(sd_keys)}"
		for k in sd_keys_hf:
			if any(k.endswith(w) for w in transposed):
				# special treatment for the Conv1D weights we need to transpose
				assert sd_hf[k].shape[::-1] == sd[k].shape
				with torch.no_grad():
					sd[k].copy_(sd_hf[k].t())
			else:
				assert sd_hf[k].shape == sd[k].shape
				with torch.no_grad():
					sd[k].copy_(sd_hf[k])

		return model

# ---------------------------------------------------------------------------------------

def load_tokens(filename):
	npt = np.load(filename)
	npt = npt.astype(np.int32)
	ptt = torch.tensor(npt, dtype=torch.long)
	return ptt


class DataLoaderLite:
	def __init__(self, B, T, process_rank, num_processes, split):
		self.B = B
		self.T = T
		self.process_rank = process_rank
		self.num_processes = num_processes
		assert split in {'train', 'val'}


		# get the shard filename
		data_root = "edu_fineweb10B"
		shards = os.listdir(data_root)
		shards = [s for s in shards if split in s]
		shards = sorted(shards)
		shards = [os.path.join(data_root, s) for s in shards]
		self.shards = shards
		assert len(shards) > 0, f"no shards found for split {split}"
		if master_process:
			print(f"found {len(shards)} shards for split {split}")
		self.reset()

	def reset(self):
		# state, init at shard zero
		self.current_shard = 0
		self.tokens = load_tokens(self.shards[self.current_shard])
		self.current_position = self.B * self.T * self.process_rank

	def next_batch(self):
		B, T = self.B, self.T
		buf = self.tokens[self.current_position:self.current_position + B * T + 1]
		x = (buf[:-1]).view(B, T) # inputs
		y = (buf[1:]).view(B, T) # targets

		self.current_position += B * T * self.num_processes

		if self.current_position + (B * T * self.num_processes + 1) > len(self.tokens):
			self.current_shard = (self.current_shard + 1) % len(self.shards)
			self.tokens = load_tokens(self.shards[self.current_shard])
			self.current_position = B * T * self.process_rank
		return x, y


# ---------------------------------------------------------------------------------------
# simple launch
# python train_gpt2.python
# DDP launch for e.g. 9 GPUs:
# torchrun --standalone --nproc_per_node=8 train_gpt2.py

# run the training loop
from torch.distributed import init_process_group, destroy_process_group
from torch.nn.parallel import DistributedDataParallel as DDP
import torch.distributed as dist

# set up DDP (distributed data parallel)
# torchrun command sets the env variables RANK, LOCAL_RANK, and WORLD_SIZE
ddp = int(os.environ.get('RANK', -1)) != -1 # is this a ddp run?
if ddp:
	# use of DDP atm demands CUDA, we set the device appropriately according to rank
	assert torch.cuda.is_available(), "for now i think we need CUDA for DDP"
	init_process_group(backend='nccl')
	ddp_rank = int(os.environ['RANK'])
	ddp_local_rank = int(os.environ['LOCAL_RANK'])
	ddp_world_size = int(os.environ['WORLD_SIZE'])
	device = f'cuda:{ddp_local_rank}'
	torch.cuda.set_device(device)
	master_process = ddp_rank == 0 # this process will do logging, checkpointing, etc
else:
	# vanilla, non-DDP run
	ddp_rank = 0
	ddp_local_rank = 0
	ddp_world_size = 1
	master_process = True
	# attempt to autodetect device
	device = "cpu"
	if torch.cuda.is_available():
		device = "cuda"
	elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
		device = "mps"
	print(f"using device: {device}")



# set seed for reproducibility
torch.manual_seed(1337)
if torch.cuda.is_available():
	torch.cuda.manual_seed(1337)

enc = tiktoken.get_encoding("gpt2")

total_batch_size = 524288 # 2^19, ~0.5M, in number of tokens
B = 64 # micro batch size
T = 1024 # sequence length
assert total_batch_size % (B * T * ddp_world_size) == 0, "make sure total_batch_size is divisible by B * T * ddp_world_size"
grad_accum_steps = total_batch_size // (B * T * ddp_world_size)
if master_process:
	print(f"total desired batch size: {total_batch_size}")
	print(f"=> calculated gradient accumulation steps: {grad_accum_steps}")


train_loader = DataLoaderLite(B=B, T=T, process_rank=ddp_rank, num_processes=ddp_world_size, split="train")
val_loader = DataLoaderLite(B=B, T=T, process_rank=ddp_rank, num_processes=ddp_world_size, split="val")

torch.set_float32_matmul_precision('high')

# create model
model = GPT(GPTConfig(vocab_size=50304))
model.to(device)
model = torch.compile(model)
if ddp:
	model = DDP(model, device_ids=[ddp_local_rank])
raw_model = model.module if ddp else model # always contains the "raw" unwrapped model


max_lr = 6e-4
min_lr = max_lr * 0.1
warmup_steps = 715 # GPT3 warms up linearly to 375M tokens. 375e6 / 524288 is 715 steps
max_steps = 19073 #10B tokens and each step does 524288
def get_lr(it):
	# 1) linear warmup for warmup_iters steps
	if it < warmup_steps:
		return max_lr * (it+1) / warmup_steps
	# 2) if it > lr_decay_iters, return min learning rate
	if it > max_steps:
		return min_lr
	# 3) in between, use cosine decay down to min learning rate
	decay_ratio = (it - warmup_steps) / (max_steps - warmup_steps)
	assert 0 <= decay_ratio <= 1
	coeff = 0.5 * (1.0 + math.cos(math.pi * decay_ratio)) # coeff starts at 1 and goes to 0
	return min_lr + coeff * (max_lr - min_lr)


# Number of parameters
print(f"Number of parameters: {sum(p.nelement() for p in model.parameters()):,}")

# optimize!
optimizer = raw_model.configure_optimizers(weight_decay=0.1, learning_rate=6e-4, device=device)
# optimizer = torch.optim.AdamW(model.parameters(), lr=3e-4, betas=(0.9, 0.95), eps=1e-8)


for step in range(max_steps):
	t0 = time.time()

	# once in a while evaluate our validatoin loss
	if step % 100 == 0:
		model.eval()
		val_loader.reset()
		with torch.no_grad():
			val_loss_accum = 0.0
			val_loss_steps = 20
			for _ in range(val_loss_steps):
				x, y = val_loader.next_batch()
				x, y = x.to(device), y.to(device)
				with torch.autocast(device_type=device, dtype=torch.bfloat16):
					logits, loss = model(x, y)
				loss = loss / val_loss_steps
				val_loss_accum += loss.detach()
		if ddp:
			dist.all_reduce(val_loss_accum, op=dist.ReduceOp.AVG)
		if master_process:
			print(f"validation loss: {val_loss_accum.item():.4f}")


	# once in a while generate from the model (except step 0 which is noise)
	# torch.compile throws an error if you don't disable
	if step > 0 and step % 100 ==0:
		model.eval()
		num_return_sequences = 4
		max_length = 32
		tokens = enc.encode("Hello, I'm a language model,")
		tokens = torch.tensor(tokens, dtype=torch.long) # (8,)
		tokens = tokens.unsqueeze(0).repeat(num_return_sequences, 1) # (4, 8)
		xgen = tokens.to(device)
		sample_rng = torch.Generator(device=device)
		sample_rng.manual_seed(42 + ddp_rank)
		while xgen.size(1) < max_length:
		    with torch.no_grad():
		        logits = model(xgen) # (B, T, vocab_size)
		        logits = logits[:, -1, :] # (B, vocab_size), take logits at last position
		        probs = F.softmax(logits, dim=-1) # get probabilities
		        topk_probs, topk_indices = torch.topk(probs, 50, dim=-1) # do top-k sampling of 50 (huggingface pipeline default), topk_probs and topk_indices become (5, 50)
		        ix = torch.multinomial(topk_probs, 1, generator=sample_rng) # (B, 1), select a token from top-k probabilities
		        xcol = torch.gather(topk_indices, -1, ix) # (B, 1), gather corresponding indices
		        xgen = torch.cat((xgen, xcol), dim=1) # append to the sequence
		        # print(x)

		for i in range(num_return_sequences):
		    tokens = x[i, :max_length].tolist()
		    decoded = enc.decode(tokens)
		    print(">", decoded)
		    print(f"rank {ddp_rank} sample {i}: {decoded}")


	# training loop
	model.train()
	optimizer.zero_grad()
	loss_accum = 0.0
	for micro_step in range(grad_accum_steps):
		x, y = train_loader.next_batch()
		x, y = x.to(device), y.to(device)
		with torch.autocast(device_type=device, dtype=torch.bfloat16):
			logits, loss = model(x, y)
			# import code; code.interact(local=locals())
		# we have to scale the loss to account for gradient accumulaton
		# because the gradients just add on each successive backward(),
		# addition of gradients correspond to a SUM in the objective, but
		# instead of a SUM we want MEAN. Scale the loss here to it comes out right
		loss = loss / grad_accum_steps
		loss_accum += loss.detach()
		if ddp:
			model.require_backward_grad_sync = (micro_step == grad_accum_steps - 1)
		loss.backward()
	if ddp:
		dist.all_reduce(loss_accum, op=dist.ReduceOp.AVG)
	norm = torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
	# determine and set the learning rate for this iteration
	lr = get_lr(step)
	for param_group in optimizer.param_groups:
		param_group['lr'] = lr
	optimizer.step()
	# torch.cuda.synchronize() # wait for the GPU to finish work
	t1 = time.time()
	dt = t1 - t0 # time diff in milliseconds
	tokens_processed = train_loader.B * train_loader.T * grad_accum_steps * ddp_world_size
	tokens_per_sec = tokens_processed / dt
	if master_process:
		print(f"step: {step} | loss: {loss_accum.item():.6f} | lr: {lr:.4e} | norm: {norm:.4f} | dt: {dt*1000:.2f}ms | tok/sec: {tokens_per_sec}")


if ddp:
	destroy_process_group()




# # GENERATION, Refactor later

# num_return_sequences = 5
# max_length = 30

# model = GPT.from_pretrained('gpt2')
# model.eval()
# model.to(device)

# import tiktoken
# enc = tiktoken.get_encoding('gpt2')
# tokens = enc.encode("Hello, I'm a language model,")
# tokens = torch.tensor(tokens, dtype=torch.long) # (8,)
# tokens = tokens.unsqueeze(0).repeat(5, 1) # (5, 8)
# x = tokens.to(device)

# torch.manual_seed(42)
# torch.cuda.manual_seed(42)
# while x.size(1) < max_length:
#     with torch.no_grad():
#         logits = model(x) # (B, T, vocab_size)
#         logits = logits[:, -1, :] # (B, vocab_size), take logits at last position
#         probs = F.softmax(logits, dim=-1) # get probabilities
#         topk_probs, topk_indices = torch.topk(probs, 50, dim=-1) # do top-k sampling of 50 (huggingface pipeline default), topk_probs and topk_indices become (5, 50)
#         ix = torch.multinomial(topk_probs, 1) # (B, 1), select a token from top-k probabilities
#         xcol = torch.gather(topk_indices, -1, ix) # (B, 1), gather corresponding indices
#         x = torch.cat((x, xcol), dim=1) # append to the sequence
#         # print(x)

# for i in range(num_return_sequences):
#     tokens = x[i, :max_length].tolist()
#     # print(tokens)
#     decoded = enc.decode(tokens)
#     print(">", decoded)















