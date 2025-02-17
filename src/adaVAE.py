#!/usr/bin/env python
#-*- coding: utf-8 -*-
"""
@file: adaVAE.py
@author: ImKe at 2021/12/26
@email: tuisaac163@gmail.com
@feature: #Enter features here
"""
import numpy as np
import collections
import torch, math, time, os, argparse, re, copy
from logger import Logger
from tensorboardX import SummaryWriter
from tqdm import tqdm
import torch.nn.functional as F
from adapters.vae import *
from utils import *
from adapters.common import AdapterConfig
from data import ConditionalGenerationDataset, GenerationDataset, GLUEPretrainingDataset
import datetime

from torch.utils.data import Dataset, DataLoader
from torch.cuda import amp
from transformers.modeling_utils import PreTrainedModel, Conv1D, prune_conv1d_layer, SequenceSummary
from transformers import GPT2Tokenizer, GPT2LMHeadModel, GPT2Config, AdamW, get_linear_schedule_with_warmup, Conv1D


parser = argparse.ArgumentParser()
scaler = amp.GradScaler()

# Default parameters are set based on single GPU training
parser.add_argument('--lr', type=float, default=5e-5)
parser.add_argument("--seed", type=int, default=42)

parser.add_argument('--model_type', type=str, default='cvae', choices=['cvae', 'vqvae', 'daae'])
parser.add_argument('--iterations', type=int, default=2000 * 3)
parser.add_argument('--dataset', type=str, default='yelp_data', choices=['yelp_data', 'yahoo_data', 'snli_data', 'claim_data',
                                                                         'penn_data', 'yelp_polarity', 'imdb_polarity', 'sst-2', 'cola'],
                    help="Dataset to use for training")
parser.add_argument('--warmup', type=int, default=1000,
                    help="Amount of iterations to warmup, then decay. (-1 for no warmup and decay)")
parser.add_argument('--pre_enc_iter', type=str, default=None,
                    help="Amount of iterations for encoder pre-training, "
                         "start for pre-training from the beginning, auto for start from totol iteration / 12"
                         " None for no pre-training")

## mode options
parser.add_argument('--adapter_size', type=int, default=256,
                    help="Hidden size of GPT2 encoder/decoder adapter")
parser.add_argument('--prefix_size', type=int, default=30,
                    help="Hidden size of GPT2 encoder/decoder prefix")
parser.add_argument('--latent_size', type=int, default=768,
                    help="Hidden size of latent code")
parser.add_argument('--encoder_n_layer', type=int, default=6,
                    help="attention layer number of GPT-2 encoder")
parser.add_argument('--decoder_n_layer', type=int, default=12,
                    help="attention layer number of GPT-2 decoder")
parser.add_argument('--class_num', type=int, default=2,
                    help="class number for controllable generation")
# parser.add_argument('--label_emb_size', type=int, default=8,
#                     help="label embedding size")
parser.add_argument('--adapter_scalar', type=str, default="1.0",
                    help="adapter scalar")
parser.add_argument('--ffn_option', type=str, default="parallel_ffn",
                    choices=['sequential_attn', 'sequential_ffn', 'parallel_attn', 'parallel_ffn', 'houlsby', 'pfeiffer'],
                    help="adapter type option")
parser.add_argument('--latent_gen', type=str, default="latent_attn",
                    help="method for encoder to latent space, averaged_attn for average attention from "
                         "TransformerCVAE, linear for taken the first encoder token to a linear like Optimus",
                    choices=['latent_attn', 'averaged_attn', 'linear', 'mean_max_linear'])
parser.add_argument('--attn_mode', type=str, default="prefix",
                    choices=['prefix', 'adapter', 'lora', 'none'],
                    help="attention transfer type")
parser.add_argument('--reg_loss', type=str, default="kld",
                    choices=['kld', 'adversarial', 'symlog', 'quantize', 'vamp'],
                    help="regularization loss for latent space")

## training paramters
parser.add_argument('--batch-sizes', nargs='+', type=int, default=[1],
                    help='batch size per GPU. Lists the schedule.')
parser.add_argument('--seq-lens', nargs='+', type=int, default=[30],
                    help='seq length per sample. Lists the schedule.')
parser.add_argument('--max_length', type=int, default=25,
                    help='max length of every input sentence')
parser.add_argument('--switch-time', type=float, default=0,
                    help="Percentage of iterations to spend on short sequence training.")
parser.add_argument('--data-dir', type=str, default='data')
parser.add_argument('--out-dir', type=str, default='out')
parser.add_argument('--from_optimus', type=str, default=None,
                    help="file to load pre-trained transformer from Optimus GPT-2")
parser.add_argument('--load_folder', type=str, default=None,
                    help="folder to load trained model weights")
parser.add_argument('--adapter_init', type=str, default='bert',
                    choices=['lora', 'bert', 'lisa', 'other'],
                    help="parameter initialization method for adapter layers.")
parser.add_argument('--workers', default=2, type=int, metavar='N',
                    help='number of data loading workers')
parser.add_argument('--early_stop', default=4, type=int,
                    help='early stopping validation step')

## metrics
parser.add_argument('--au_delta', type=float, default=0.01,
                    help="threshold for activated unit calculation. 0.01 as suggested in Optimus")

# use GPU
parser.add_argument('--gpu', default=0, type=int)
parser.add_argument('--no_gpu', action="store_true")

parser.add_argument('--fp16_opt_level', default='O1', type=str, required=False)

# KL cost annealing, increase beta from beta_0 to 1 in beta_warmup steps
parser.add_argument('--beta_0', default=1.00, type=float)
parser.add_argument('--beta_warmup', type=int, default=1000)
parser.add_argument('--kl_rate', type=float, default=0.0)
parser.add_argument('--fb', type=int, default=1, choices=[1, 2, 3, 4])

# cyc_vae parameters
parser.add_argument('--cycle', type=str, default='auto',
                    help='const for no cycle, auto for 4 cycles, precise number for concrete cycle iteration')

## trigger
parser.add_argument('--load', action="store_true")
# parser.add_argument('--label_cond', action="store_true")
parser.add_argument('--save_all', action="store_true",
                    help="save full parameters of the model, may up to 500M+")
parser.add_argument('--weighted_sample', action="store_true", default=False)
parser.add_argument('--add_input', action="store_true")
parser.add_argument('--add_attn', action="store_true")
parser.add_argument('--add_softmax', action="store_true")
parser.add_argument('--add_mem', action="store_true")
parser.add_argument('--attn_proj_vary', action="store_true")
parser.add_argument('--learn_prior', action="store_true")
parser.add_argument('--add_z2adapters', action="store_true")
parser.add_argument('--finetune_enc', help="whether to fine-tune encoder, if True, no adapter added in encoder",
                    action="store_true")
parser.add_argument('--finetune_dec', help="whether to fine-tune decoder, if True, no adapter added in decoder",
                    action="store_true")


def compute_loss(device, model, x_tokens, input_tokens, att_mask, loss_fn, beta, kl_rate, reg_loss, weighted_sample=False, from_mean=False, fb=1):
    """

    :param device:
    :param model:
    :param input_tokens: input word ids
    :param mask: input mask
    :param x_tokens: target sequence
    :param loss_fn:
    :param beta: weight of regularization loss
    :param use_adv_loss: use adversarial loss for WAE
    :return:
    """
    input_tokens = input_tokens.to(device)
    att_mask = att_mask.to(device)
    x_tokens = x_tokens.to(device)

    outputs = model(input_ids=input_tokens, attention_mask=att_mask, from_mean=from_mean)
    logits = outputs[0]
    regularization_loss = outputs[-3]
    mean = outputs[-2]
    logvar = outputs[-1]
    if reg_loss == "adversarial":
        d_loss, g_loss, kld = regularization_loss[0], regularization_loss[1], regularization_loss[2]
        regularization_loss[2] = regularization_loss[2].sum(-1)
    else:
        kl_loss = regularization_loss
        regularization_loss = regularization_loss.sum(-1)
    num_logits = logits.size(-1)

    # Perform masking
    if att_mask is not None and not weighted_sample:
        att_mask = att_mask.type(torch.bool)
        logits = logits.masked_select(att_mask.unsqueeze(-1))
        x_tokens = x_tokens.masked_select(att_mask)

    ## x_token is target tokens
    ce_loss = loss_fn(logits.view(-1, num_logits), x_tokens.view(-1))
    if reg_loss == "adversarial":
        loss = ce_loss.mean() + g_loss + beta * d_loss #+ beta * kld
    else:
        ## kl_loss: [bs, nz]
        if fb == 1:
            loss = ce_loss.mean() + beta * max(kl_loss.sum(dim=1).mean(), kl_rate)
        elif fb == 2 or fb == 4:
            kl_mask = (kl_loss > kl_rate).float().to(device)
            kl_loss = (kl_mask * kl_loss).sum(dim=1)
            # mask = (kl_loss > kl_rate).float().to(device)
            # kl_loss = kl_loss * mask + (1 - mask) * torch.full(kl_loss.size(), kl_rate).to(device)
            loss = (ce_loss.mean() + beta * kl_loss).mean()
        elif fb == 3:
            kl_mask = (kl_loss > kl_rate).float().to(device)
            ## Hinge Loss
            kl_loss = (kl_loss * kl_mask + (1.0 - kl_mask) * torch.full(kl_loss.size(), kl_rate).to(device)).sum(dim=1)
            loss = (ce_loss.mean() + beta * kl_loss).mean()

    if weighted_sample:
        nsamples = 100
        ns = 10
        bs = mean.size(0)
        ll_tmp, rc_tmp = [], []
        x_tokens = x_tokens.unsqueeze(1).expand(bs, ns, x_tokens.size(-1)).contiguous()
        for _ in range(int(nsamples / ns)):
            # (batch, nsamples, nz)
            z = model.reparameterize(mean, logvar, ns=ns)

            # [batch, nsamples]
            log_prior = (-0.5 * math.log(2 * math.pi) - z ** 2 / 2).sum(dim=-1)#prior.log_prob(z).sum(dim=-1) ## [bs, ns]
            logits = model.eval_cond_ll(x=input_tokens, mask=att_mask, z=z)
            log_gen = - loss_fn(logits.view(-1, logits.size(-1)), x_tokens.view(-1)).view(bs, ns, -1).sum(-1)

            log_infer = model.eval_inference_dist(z, (mean, logvar)) # [bs, ns]

            # pdb.set_trace()
            # log_gen = log_gen.unsqueeze(0).contiguous().view(z.shape[0], -1)
            # pdb.set_trace()
            rc_tmp.append(log_gen)
            ll_tmp.append(log_gen - log_prior + log_infer)

        log_prob_iw = log_sum_exp(torch.cat(ll_tmp, dim=-1), dim=-1) - math.log(nsamples)
        log_gen_iw = torch.mean(torch.cat(rc_tmp, dim=-1), dim=-1)
        return loss, ce_loss, regularization_loss, mean, logvar, log_prob_iw, log_gen_iw
    else:
        return loss, ce_loss, regularization_loss, mean, logvar

def train_step(device, model, optimizer, x_tokens, input_tokens, att_mask, loss_fn, beta, kl_rate, reg_loss_type, from_mean, fb):
    optimizer.zero_grad()
    loss, ce_loss, reg_loss, _, _ = compute_loss(device, model, x_tokens, input_tokens, att_mask, loss_fn,
                                          beta, kl_rate, reg_loss_type, weighted_sample=False, from_mean=from_mean, fb=fb)
    scaler.scale(loss).backward()
    scaler.unscale_(optimizer)
    torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)  # max_grad_norm=1.0
    scaler.step(optimizer)
    scaler.update()
    # output.append((loss.item(), ce_loss.mean().item(), reg_loss.item()))
    if reg_loss_type == "adversarial":
        ## reg_loss: [discriminator loss, generator loss, KL loss]
        reg_loss[2] = reg_loss[2].mean()
    else:
        reg_loss = reg_loss.mean()
    return loss.item(), ce_loss.mean().item(), reg_loss

def train(args):
    now = datetime.datetime.now()
    # GPU
    if not torch.cuda.is_available(): args.no_gpu = True
    gpu = not args.no_gpu
    if gpu:
        print("There are ", torch.cuda.device_count(), " available GPUs!")
        torch.cuda.set_device(args.gpu)
        print('Current single GPU: {}'.format(torch.cuda.current_device()))
    device = torch.device(args.gpu if gpu else "cpu")

    # randomness
    np.random.seed(args.seed)
    prng = np.random.RandomState()
    torch.random.manual_seed(args.seed)
    if gpu: torch.cuda.manual_seed(args.seed); torch.cuda.manual_seed_all(args.seed)

    if args.add_attn and args.add_mem:
        fusion_type = "add_attn_mem"
    elif args.add_attn and not args.add_mem and not args.add_z2adapters:
        fusion_type = "add_attn"
    elif args.add_attn and args.add_z2adapters:
        fusion_type = "add_attn_adap"
    elif args.add_mem and not args.add_attn:
        fusion_type = "add_mem"
    elif args.add_input:
        fusion_type = "add_input"
    elif args.add_ouput:
        fusion_type = "add_ouput"

    opt = True if not args.from_optimus is None else False
    ws = True if args.weighted_sample else False
    fine_tune = False
    if args.finetune_enc and args.finetune_dec:
        fine_tune = True

    parameter_efficient_size = f"{args.adapter_size}-{args.prefix_size}" if args.attn_mode == "prefix" else f"{args.adapter_size}"
    # logging
    experiment = f"{args.dataset}_iter{args.iterations}_as{parameter_efficient_size}_scalar{args.adapter_scalar}_cycle-{args.cycle}_prenc-{args.pre_enc_iter}_ws{ws}" \
                 f"_lg-{args.latent_gen}_{fusion_type}_beta{args.beta_0}_reg-{args.reg_loss}_attn_mode-{args.attn_mode}_ffn_option-{args.ffn_option}_" \
                 f"enc_layer-{args.encoder_n_layer}_dec_layer-{args.decoder_n_layer}_zdim-{args.latent_size}_opt{opt}_ft{fine_tune}_zrate-{args.kl_rate}_fb-{args.fb}" \
                 f"sd-{args.seed}_{now.month}.{now.day}"
    save_folder = os.path.join(args.out_dir, experiment)
    os.makedirs(os.path.join(save_folder, 'ckpt/model'), exist_ok=True)
    os.makedirs(os.path.join(save_folder, 'ckpt/opt'), exist_ok=True)
    t_writer = SummaryWriter(os.path.join(save_folder, 'train'), flush_secs=5)
    v_writer = SummaryWriter(os.path.join(save_folder, 'val'), flush_secs=5)
    # importlib.reload(logging)
    logging_file = f"{args.dataset}_init-{args.adapter_init}_ada-scalar{args.adapter_scalar}_cycle-{args.cycle}_prenc-{args.pre_enc_iter}_as{parameter_efficient_size}_ws{ws}" \
                   f"_lg-{args.latent_gen}_{fusion_type}_beta{args.beta_0}_reg-{args.reg_loss}_attn_mode-{args.attn_mode}_ffn_option-{args.ffn_option}_" \
                   f"beta{args.beta_0}_enc_layer-{args.encoder_n_layer}_dec_layer-{args.decoder_n_layer}_zdim-{args.latent_size}_opt{opt}_ft{fine_tune}_z" \
                   f"rate-{args.kl_rate}_fb-{args.fb}_sd-{args.seed}_{now.month}.{now.day}.log"
    logging = Logger(os.path.join(save_folder, logging_file))
    logging.info('\n*******************************************************************************\n')
    logging.info("the configuration:")
    logging.info(str(args).replace(',', '\n'))

    logging.info('Loading models...')

    ## GPT2 config and adapter config
    config = GPT2Config()
    """
    GPT2Config {
      "activation_function": "gelu_new",
      "attn_pdrop": 0.1,
      "bos_token_id": 50256,
      "embd_pdrop": 0.1,
      "eos_token_id": 50256,
      "initializer_range": 0.02,
      "layer_norm_epsilon": 1e-05,
      "model_type": "gpt2",
      "n_ctx": 1024,
      "n_embd": 768,
      "n_head": 12,
      "n_inner": null,
      "n_layer": 12,
      "n_positions": 1024,
      "reorder_and_upcast_attn": false,
      "resid_pdrop": 0.1,
      "scale_attn_by_inverse_layer_idx": false,
      "scale_attn_weights": true,
      "summary_activation": null,
      "summary_first_dropout": 0.1,
      "summary_proj_to_labels": true,
      "summary_type": "cls_index",
      "summary_use_proj": true,
      "transformers_version": "4.12.0",
      "use_cache": true,
      "vocab_size": 50257
    }
    """
    ada_config = AdapterConfig(hidden_size=768,
                               adapter_size=args.adapter_size, # adapter hidden size, larger will activate more trainable parameters
                               adapter_act='relu',
                               adapter_initializer_range=1e-2,
                               latent_size=args.latent_size, # latent dimension (32 for language modeling, 728 for interpolation)
                               class_num=args.class_num, # class number for controllable generation
                               encoder_n_layer=args.encoder_n_layer,
                               decoder_n_layer=args.decoder_n_layer,
                               dis_emb=128, # hidden dimension for adversarial KLD discriminator
                               init=args.adapter_init, # adapter initialization method
                               adapter_scalar=args.adapter_scalar,
                               ffn_option=args.ffn_option,
                               attn_mode=args.attn_mode,
                               latent_gen=args.latent_gen,
                               attn_option='none',
                               mid_dim=args.prefix_size,
                               attn_bn=25,
                               prefix_dropout=0.1,
                               tune_enc=args.finetune_enc,
                               tune_dec=args.finetune_dec,
                               add_z2adapters=args.add_z2adapters)

    tokenizer = GPT2Tokenizer.from_pretrained('gpt2')

    # Hack to allow tokenizing longer sequences.
    # tokenizer.max_len = int(1e12)
    if args.from_optimus is None:
        gpt2_model = GPT2LMHeadModel.from_pretrained('gpt2')
        tokenizer.pad_token = tokenizer.eos_token
    else:
        logging.info("Loading Pre-trained weights from Optimus GPT-2")
        optimus_gpt2_state_dict = torch.load(args.from_optimus)
        gpt2_model = GPT2LMHeadModel(config)
        special_tokens_dict = {'pad_token': '<PAD>', 'bos_token': '<BOS>', 'eos_token': '<EOS>'}
        num_added_toks = tokenizer.add_special_tokens(special_tokens_dict)
        print('We have added', num_added_toks, 'tokens to GPT2')
        # Notice: resize_token_embeddings expect to receive the
        # full size of the new vocabulary, i.e. the length of the tokenizer.
        gpt2_model.resize_token_embeddings(len(tokenizer))
        assert tokenizer.pad_token == '<PAD>'
        _ = gpt2_model.load_state_dict(optimus_gpt2_state_dict, strict=False)
    endoftext = tokenizer.convert_tokens_to_ids(tokenizer.eos_token)

    logging.info(f'gpt2_params:{num_params(gpt2_model)}') # gpt2: 124439808
    logging.info(f'gpt2_transformer_params:{num_params(gpt2_model.transformer)}')


    AdaVAE = AdaVAEModel(config, ada_config, add_input=args.add_input, add_attn=args.add_attn, add_softmax=args.add_softmax, add_mem=args.add_mem,
                   attn_proj_vary=args.attn_proj_vary, learn_prior=args.learn_prior, reg_loss=args.reg_loss)
    if not args.from_optimus is None:
        AdaVAE.encoder.resize_token_embeddings(len(tokenizer))
        AdaVAE.transformer.resize_token_embeddings(len(tokenizer))
    init_para_frompretrained(AdaVAE.transformer, gpt2_model.transformer, share_para=True)
    init_para_frompretrained(AdaVAE.encoder, gpt2_model.transformer, share_para=True)

    ## freeze all prarameters excpect the ones in adapters
    # AdaVAE = freeze_all_parameters(AdaVAE)
    # AdaVAE.transformer = unfreeze_GPT2_adapters(AdaVAE.transformer, Cond_GPT2Adapter)
    # AdaVAE.encoder = unfreeze_GPT2_adapters(AdaVAE.encoder, Cond_GPT2Adapter)

    AdaVAE.lm_head.weight = gpt2_model.lm_head.weight
    if AdaVAE.add_softmax:
        AdaVAE.lm_head_rep = Conv1D(*gpt2_model.lm_head.weight.size())
        # AdaVAE.lm_head_rep = LM_head_rep(*gpt2_model.lm_head.weight.size()[::-1])
    adavae_params = num_params(AdaVAE)
    logging.info(f'AdaVAE params: {adavae_params}')

    # fix pre-trained parameters before certain iterations
    tuning_all_after_iters = int(args.iterations / 6)
    args.warmup = args.beta_warmup = int(args.iterations / 6)
    cycle_num = int(args.iterations / 4) if args.cycle == 'auto' or args.cycle == 'const' else int(args.cycle)
    if args.pre_enc_iter is None:
        pre_enc_iter = tuning_all_after_iters #int(args.iterations / 3)
    elif args.pre_enc_iter == "start":
        pre_enc_iter = 0
    elif args.pre_enc_iter == "auto":
        pre_enc_iter = tuning_all_after_iters / 2
    else:
        pre_enc_iter = int(args.pre_enc_iter)
    assert pre_enc_iter <= tuning_all_after_iters, "Encoder pre-training should start before whole model tuning."
    ## set flag
    tuning_all = False # tune the whole model
    tuning_enc = False # pre-train the encoder to avoid KL Collapse
    for name, parameter in AdaVAE.named_parameters():
        new_pars = ['attention_weights', 'mean', 'logvar', 'input_proj', 'attn_proj', 'Nu_fc1', 'Nu_fc2',
                    'lm_head_rep', 'z_linear', 'discriminator', 'latent2mem', 'c_z', 'linear_trans']

        if not any([True if n in name else False for n in new_pars]):
            parameter.requires_grad = False
        print((name, parameter.requires_grad))
    # print(AdaVAE)
    logging.info(f'AdaVAE params with gradients: {num_params(AdaVAE)}')

    logging.info('Setup data...')
    # Batch and sequence length schedule
    assert len(args.batch_sizes) == len(args.seq_lens)
    batch_schedule = list(zip(map(int, args.batch_sizes), map(int, args.seq_lens)))
    assert len(batch_schedule) <= 2, 'Currently not supporting multiple schedule'
    args.switch_time = 0
    cur_b_schedule = len(batch_schedule) - 1 if args.switch_time == 0 else 0
    logging.info('Batch schedule')
    logging.info(batch_schedule)

    if args.dataset in ['sst-2', 'cola']:
        prefix_path = "../low_nlu/glue_data"
        GDataset = GLUEPretrainingDataset
        train_set = GDataset.from_file(os.path.join(prefix_path, args.dataset.upper(), "train.tsv"), args.dataset)
        test_set = GDataset.from_file(os.path.join(prefix_path, args.dataset.upper(), "dev.tsv"), args.dataset)
        val_set = GDataset.from_file(os.path.join(prefix_path, args.dataset.upper(), "dev.tsv"), args.dataset)
    else:
        if args.dataset in ['yelp_polarity', 'imdb_polarity']:
            prefix_path = "../data"
            GDataset = ConditionalGenerationDataset
        else:
            prefix_path = "../data/optimus_dataset"
            if args.dataset in ['yelp_data', 'claim_data']:
                GDataset = ConditionalGenerationDataset
            else:
                GDataset = GenerationDataset
        train_set = GDataset.from_file(os.path.join(prefix_path, args.dataset, "train.txt"))
        test_set = GDataset.from_file(os.path.join(prefix_path, args.dataset, "test.txt"))
        val_set = GDataset.from_file(os.path.join(prefix_path, args.dataset, "valid.txt"))

    train_loader = DataLoader(
        train_set,
        batch_size=batch_schedule[cur_b_schedule][0],
        pin_memory=True,
        drop_last=True,
        num_workers=args.workers,
        shuffle=True)
    test_bs = 10 if args.weighted_sample else batch_schedule[-1][0]
    test_loader = DataLoader(
        test_set,
        batch_size=test_bs,
        pin_memory=True,
        drop_last=True,
        num_workers=args.workers,
        shuffle=True)
    val_loader = DataLoader(
        val_set,
        batch_size=test_bs,
        pin_memory=True,
        drop_last=True,
        num_workers=args.workers,
        shuffle=True)
    logging.info('Done.')

    logging.info('Wrapping models and optimizers...')
    # Apply linear scaling rule to increase batch size for short sequence training.
    lr_schedule = switch_schedule(linear_schedule(args), batch_schedule[cur_b_schedule][0] / batch_schedule[-1][0],
                                  int(args.iterations * args.switch_time))

    # add_special_tokens_(tokenizer, AdaVAE)
    AdaVAE = AdaVAE.to(device)
    AdaVAE.train()

    optimizer = AdamW(AdaVAE.parameters(), lr=args.lr, correct_bias=True)
    scheduler = torch.optim.lr_scheduler.LambdaLR(optimizer, lr_schedule)
    ## load ckpt
    if args.load:
        logging.info('Loading model weights...')
        state = torch.load(os.path.join(args.load_folder, "model_best_val.pt"))  # , map_location='cpu' model_latest.pt
        if 'module' in list(state.keys())[0]:  # model_path is data parallel model with attr 'module'
            state_copy = copy.copy(state)
            keys = state_copy.keys()
            for k in keys:
                state[k.replace('module.', '')] = state.pop(k)
        ## load trained parameters
        if not args.save_all:
            model_dict = AdaVAE.state_dict()
            additional_dict = {k: v for k, v in state.items() if k in model_dict}
            model_dict.update(additional_dict)
            AdaVAE.load_state_dict(model_dict)
            del model_dict
        else:
            AdaVAE.load_state_dict(state)
            del state
        # gc.collect()
        logging.info('Done.')
    loss_fn = nn.CrossEntropyLoss(ignore_index=endoftext, reduction='none')

    logging.info("Begin training iterations")
    max_val_batches = 200  # max num. of val batches
    logging.info("Total iteration: %d" % args.iterations)
    e = 0  # number of epoch
    num_iters = 0
    optimizer.zero_grad()
    beta = args.beta_0

    def val_step(val_loader):
        AdaVAE.eval()

        n_words_bpe = 0
        n_words = 0
        n_examples = 0
        cnt_au = 0
        logp_sum = 0.
        reg_loss_sum = 0.
        length_sum = 0.

        if args.weighted_sample:
            reported_loss_ppl = 0.
            reported_loss_rec = 0.

        if args.reg_loss == "adversarial":
            d_loss_sum = 0.
            g_loss_sum = 0.

        mu_batch_list, logvar_batch_list = [], []
        neg_entropy = 0.

        logging.info("Validation loop.         Batches: %d" % len(val_loader))
        logging.info("Validation loop. max_val_batches: %d" % max_val_batches)

        with tqdm(total=min(len(val_loader), max_val_batches), desc="Evaluating Model") as pbar:
            for i, val_data_dict in enumerate(val_loader):
                with torch.no_grad():
                    val_x_ids, val_input_ids, val_attention_mask = tokenize(val_data_dict['x'], tokenizer, device, args)
                    if args.weighted_sample:
                        val_loss, val_ce_loss, val_reg_loss, val_mu, val_lv, \
                        val_loss_ppl, val_loss_rec = compute_loss(device, AdaVAE, val_x_ids,
                                                                       val_input_ids, val_attention_mask,
                                                                       loss_fn, 1.0, 0.0, args.reg_loss,
                                                                  weighted_sample=True, fb=args.fb)
                        val_loss_ppl, val_loss_rec = val_loss_ppl.sum(), val_loss_rec.mean()
                        reported_loss_ppl += val_loss_ppl.item()
                        reported_loss_rec += val_loss_rec.item()
                    else:
                        val_loss, val_ce_loss, val_reg_loss, val_mu, val_lv = compute_loss(device, AdaVAE, val_x_ids,
                                                                                           val_input_ids,
                                                                                           val_attention_mask,
                                                                                           loss_fn, 1.0, 0.0,
                                                                                           args.reg_loss, fb=args.fb)
                """
                calculate text perplexity
                """
                target_tokens = val_x_ids
                if len(target_tokens.size()) == 1:
                    target_tokens = target_tokens.unsqueeze(0)
                n, l = target_tokens.size()

                text = target_tokens.tolist()
                tokens = [t[:t.index(endoftext) + 1] if endoftext in t else t for t in text]
                words_bpe = sum([len(t) for t in tokens])
                # length_sum += (l - 1) * n
                n_words_bpe += words_bpe
                logprob = val_ce_loss.mean()


                logp_sum += logprob * words_bpe

                ## add special token (batch size)
                n_words_bpe += len(text)

                ctext = [tokenizer.decode(target_tokens[i, :], clean_up_tokenization_spaces=True) for i in range(n)]
                ctext = [s[s.find("<|endoftext|>") + len("<|endoftext|>"):] for s in ctext]
                ctext = [s[:s.find("<|endoftext|>") + len("<|endoftext|>")] if "<|endoftext|>" in s else s for s in ctext]
                words = sum([len(
                    [t for t in re.split('("|\'|!|\?|\.|,|:| |\n|’|“|”|;|\(|\)|`)', s) if t != ' ' and t != '']) for
                    s in ctext])
                n_words += words

                if args.reg_loss == "adversarial":
                    d_loss, g_loss, kld = val_reg_loss[0].item(), val_reg_loss[1].item(), val_reg_loss[2]
                    reg_loss_sum += kld.mean().item()
                    d_loss_sum += d_loss
                    g_loss_sum += g_loss
                else:
                    reg_loss_sum += val_reg_loss.mean().item()

                """
                calculate mutual information (mi) Stage 1
                """
                n_examples += n
                nz = val_mu.size(1)
                # E_{q(z|x)}log(q(z|x)) = -0.5*nz*log(2*\pi) - 0.5*(1+logvar).sum(-1)
                neg_entropy += (-0.5 * nz * math.log(2 * math.pi) - 0.5 * (1 + val_lv).sum(-1)).sum().item()
                mu_batch_list += [val_mu.cpu()]
                logvar_batch_list += [val_lv.cpu()]

                """
                compute the number of active units (au) Stage 1
                """
                if cnt_au == 0:
                    means_sum = val_mu.sum(dim=0, keepdim=True)
                else:
                    means_sum = means_sum + val_mu.sum(dim=0, keepdim=True)
                cnt_au += val_mu.size(0)


                if i > max_val_batches:
                    break
                pbar.update(1)

        val_loader_len = min(len(val_loader), max_val_batches)
        neg_entropy = neg_entropy / n_examples
        mean_mean = means_sum / cnt_au
        loss_bpe = logp_sum / n_words_bpe # nll
        reg = reg_loss_sum / val_loader_len
        latent_bound = g_loss if args.reg_loss == "adversarial" else reg_loss_sum
        if args.weighted_sample:
            elbo = (reg_loss_sum - reported_loss_rec) / val_loader_len
            nll = - reported_loss_rec / val_loader_len
            neg_reported_ppl_loss = -reported_loss_ppl
            ppl_bpe = round(math.exp(min(neg_reported_ppl_loss / n_words_bpe, 100)), 3)
            ppl_elbo = round(math.exp(min((reg_loss_sum - reported_loss_rec) / n_words_bpe, 100)), 3)
            ppl_word = round(math.exp(min(neg_reported_ppl_loss / n_words, 100)), 3)
        else:
            elbo = (logp_sum + latent_bound) / n_words_bpe
            ppl_bpe = round(math.exp(min((logp_sum + latent_bound) / n_words_bpe, 100)), 3)
            ppl_word = round(math.exp(min((logp_sum + latent_bound) / n_words, 100)), 3)
        if args.reg_loss == "adversarial":
            d_loss = d_loss_sum / val_loader_len
            g_loss = g_loss_sum / val_loader_len

        """
        calculate mi and au Stage 2
        """
        n_examples = 0
        log_qz = 0.
        for i in tqdm(range(len(mu_batch_list)), desc="Evaluating MI, Stage 2"):
            ###############
            # get z_samples
            ###############
            mu, logvar = mu_batch_list[i].cuda(), logvar_batch_list[i].cuda()

            # [z_batch, 1, nz]
            with torch.no_grad():
                z_samples = AdaVAE.reparameterize(mu, logvar).unsqueeze(1)

            z_samples = z_samples.view(-1, 1, nz)
            n_examples += z_samples.size(0)

            ###############
            # compute density
            ###############
            # [1, x_batch, nz]
            # mu, logvar = mu_batch_list[i].cuda(), logvar_batch_list[i].cuda()
            # indices = list(np.random.choice(np.arange(len(mu_batch_list)), 10)) + [i]
            indices = np.arange(len(mu_batch_list))
            mu = torch.cat([mu_batch_list[_] for _ in indices], dim=0).cuda()
            logvar = torch.cat([logvar_batch_list[_] for _ in indices], dim=0).cuda()
            x_batch, nz = mu.size()

            mu, logvar = mu.unsqueeze(0), logvar.unsqueeze(0)
            var = logvar.exp()

            # (z_batch, x_batch, nz)
            dev = z_samples - mu

            # (z_batch, x_batch)
            log_density = -0.5 * ((dev ** 2) / var).sum(dim=-1) - \
                          0.5 * (nz * math.log(2 * math.pi) + logvar.sum(-1))

            # log q(z): aggregate posterior
            # [z_batch]
            log_qz += (log_sum_exp(log_density, dim=1) - math.log(x_batch)).sum(-1)


        log_qz /= n_examples
        mi = (neg_entropy - log_qz).item()

        """
        calculate au Stage 2
        """
        cnt_au = 0
        with tqdm(total=min(len(val_loader), max_val_batches), desc="Evaluating AU, Stage 2") as pbar:
            for i, val_data_dict in enumerate(val_loader):
                with torch.no_grad():
                    val_x_ids, val_input_ids, val_attention_mask = tokenize(val_data_dict['x'], tokenizer, device, args)

                    val_loss, val_ce_loss, _, val_mu, val_lv = compute_loss(device, AdaVAE, val_x_ids,
                                                                            val_input_ids, val_attention_mask,
                                                                            loss_fn, 1.0, 0.0, args.reg_loss, fb=args.fb)
                if cnt_au == 0:
                    var_sum = ((val_mu - mean_mean) ** 2).sum(dim=0)
                else:
                    var_sum = var_sum + ((val_mu - mean_mean) ** 2).sum(dim=0)
                cnt_au += val_mu.size(0)

                if i > max_val_batches:
                    break
                pbar.update(1)

        # (nz)
        au_var = var_sum / (cnt_au - 1)
        n_au = (au_var >= args.au_delta).sum().item()



        v_writer.add_scalar('loss', loss_bpe, num_iters)
        v_writer.add_scalar('elbo', elbo, num_iters)
        v_writer.add_scalar('ppl_bpe', ppl_bpe, num_iters)
        v_writer.add_scalar('ppl_elbo', ppl_elbo, num_iters)
        v_writer.add_scalar('ppl_word', ppl_word, num_iters)
        v_writer.add_scalar('reg_loss', reg, num_iters)
        v_writer.add_scalar('mutual_information', mi, num_iters)
        v_writer.add_scalar('activagte_unit', n_au, num_iters)
        if args.reg_loss == "adversarial":
            v_writer.add_scalar('d_loss', d_loss, num_iters)
            v_writer.add_scalar('g_loss', g_loss, num_iters)
            logging.info('val d_loss: %.4f' % d_loss)
            logging.info('val g_loss: %.4f' % g_loss)
        if args.weighted_sample:
            v_writer.add_scalar('nll', nll, num_iters)
            logging.info('val nll  : %.4f' % nll)
        logging.info('val loss     : %.4f' % loss_bpe)
        logging.info('val elbo     : %.4f' % elbo)
        logging.info('val ppl_bpe  : %.4f' % ppl_bpe)
        logging.info('val ppl_elbo : %.4f' % ppl_elbo)
        logging.info('val ppl_word : %.4f' % ppl_word)
        logging.info('val reg_loss : %.4f' % reg)
        logging.info('val MI       : %.4f' % mi)
        logging.info('val AU       : %.4f' % n_au)
        bsz = 5
        sents, _ = sample_sequence(AdaVAE, args.max_length,
                                batch_size=bsz, top_k=100, top_p=0.95,
                                device=device, sample=True, eos_token=endoftext)
        # Sample sentences
        logging.info("-" * 50)
        sents = sents.tolist()
        for i in range(len(sents)):
            sent = sents[i]
            sent = sent[sent.index(endoftext) + 1:]

            if endoftext in sent:
                idx = sent.index(endoftext)
                sent = sent[:idx]

            sent = tokenizer.decode(sent, clean_up_tokenization_spaces=True).strip()
            logging.info(sent)

        AdaVAE.train()
        return nll, loss_bpe, elbo, ppl_bpe, ppl_elbo, ppl_word, reg, mi, n_au

    cyclic_weights = frange_cycle_zero_linear(args.iterations+1, start=0.0, stop=args.beta_0,
                                              n_cycle=4, ratio_increase=0.25, ratio_zero=0.25) #frange_cycle_linear(args.iterations, start=0.0, stop=args.beta_0, n_cycle=4, ratio=0.5)

    best_val_nll, best_val_loss_bpe, best_val_elbo, best_val_ppl_bpe, best_val_ppl_elbo, \
    best_val_ppl_word, best_val_reg, best_val_mi, best_val_n_au = 99999., 0., 0., 0., 0., 0., 0., 0., 0.
    et = 0
    while (num_iters < args.iterations) and et < args.early_stop:
        # Run epoch
        st = time.time()

        # Training
        print('Training loop. Batches:', len(train_loader))
        logging.info('\n----------------------------------------------------------------------')
        logging.info("Training loop.       Batches: %d" % len(train_loader))

        with tqdm(total=len(train_loader)) as pbar:
            for i, data_dict in enumerate(train_loader):
                x_ids, input_ids, attention_mask = tokenize(data_dict['x'], tokenizer, device, args)

                # if (args.cycle != "const") and (num_iters % cycle_num >= cycle_num - args.beta_warmup):
                #     beta = min(1.0, beta + (1. - args.beta_0) / args.beta_warmup)
                beta = cyclic_weights[num_iters]

                if not tuning_enc and num_iters >= pre_enc_iter:
                    encoder_unfreeze_modules = [GPT2Adapter]
                    if ada_config.attn_mode == "prefix":
                        encoder_unfreeze_modules.append(Prefix)
                    AdaVAE.encoder = unfreeze_GPT2_adapters(AdaVAE.encoder, encoder_unfreeze_modules)
                    logging.info("Start pre-training the encoder.")
                    tuning_enc = True

                if not tuning_all and num_iters >= tuning_all_after_iters:
                    decoder_unfreeze_modules = [GPT2Adapter]
                    if ada_config.attn_mode == "prefix":
                        decoder_unfreeze_modules.append(Prefix)
                    AdaVAE.transformer = unfreeze_GPT2_adapters(AdaVAE.transformer, decoder_unfreeze_modules)
                    if args.finetune_enc or args.finetune_dec:
                        if args.finetune_enc:
                            for _, parameter in AdaVAE.encoder.named_parameters():
                                parameter.requires_grad = True
                        if args.finetune_dec:
                            for _, parameter in AdaVAE.transformer.named_parameters():
                                parameter.requires_grad = True
                    # for name, parameter in AdaVAE.named_parameters():
                    #     print((name, parameter.requires_grad))
                    adavae_params_with_gradients = num_params(AdaVAE)
                    logging.info(f'AdaVAE params with gradients:{adavae_params_with_gradients}')
                    if args.finetune_enc or args.finetune_dec:
                        logging.info('Trainable parameters %d / %d= %.4f'%(adavae_params_with_gradients, adavae_params,
                                                                             adavae_params_with_gradients/adavae_params))
                    else:
                        logging.info('Additional parameters %d / %d = %.4f'%(adavae_params_with_gradients, adavae_params,
                                                                             adavae_params_with_gradients/(adavae_params - adavae_params_with_gradients)))
                    tuning_all = True

                if args.warmup != -1:
                    scheduler.step()
                if args.fb == 1:
                    kl_rate = args.kl_rate * args.latent_size
                elif args.fb == 4:
                    kl_rate = args.kl_rate / args.latent_size
                else:
                    kl_rate = args.kl_rate
                loss, ce_loss, regul_loss = train_step(device, AdaVAE, optimizer, x_ids, input_ids, attention_mask,
                                                       loss_fn, beta, kl_rate, args.reg_loss, False, args.fb)
                if args.reg_loss == "adversarial":
                    d_loss, g_loss, kld = regul_loss[0].item(), regul_loss[1].item(), regul_loss[2].item()
                else:
                    kld = regul_loss.item()

                lr = scheduler.get_last_lr()[0]
                # Log to Tensorboard
                t_writer.add_scalar('loss', loss, num_iters)
                t_writer.add_scalar('ppl', math.exp(min(ce_loss, 10)), num_iters)
                t_writer.add_scalar('lr', lr, num_iters)
                t_writer.add_scalar('iter_time', time.time() - st, num_iters)
                t_writer.add_scalar('kl', kld, num_iters)
                if args.reg_loss == "adversarial":
                    t_writer.add_scalar('d_loss', d_loss, num_iters)
                    t_writer.add_scalar('g_loss', g_loss, num_iters)
                t_writer.add_scalar('beta', beta, num_iters)

                st = time.time()
                end = num_iters >= args.iterations


                if end:
                    break
                num_iters += 1
                pbar.update(1)

                log_interval = int(args.iterations / 5)
                if num_iters % log_interval == 0:
                    logging.info("test set")
                    _ = val_step(test_loader)
                    logging.info("validation set")
                    val_nll, val_loss_bpe, val_elbo, val_ppl_bpe, val_ppl_elbo, val_ppl_word, \
                    val_reg, val_mi, val_n_au = val_step(val_loader)
                    if val_nll < best_val_nll:
                        best_val_nll, best_val_loss_bpe, best_val_elbo, best_val_ppl_bpe, best_val_ppl_elbo, \
                        best_val_ppl_word, best_val_reg, best_val_mi, best_val_n_au = val_nll, val_loss_bpe, val_elbo, \
                                                                                      val_ppl_bpe, val_ppl_elbo, val_ppl_word, val_reg, val_mi, val_n_au
                        if args.save_all:
                            save_orderdict = AdaVAE.state_dict()
                        else:
                            save_orderdict = collections.OrderedDict()
                            for name, parameter in AdaVAE.named_parameters():
                                if parameter.requires_grad:
                                    save_orderdict[name] = parameter
                        logging.info("Saving model w.r.t the best nll.")
                        torch.save(save_orderdict, os.path.join(save_folder, 'model_best_val.pt'))
                    else:
                        et += 1

                if num_iters % int(args.iterations / 0.5) == 0:
                    logging.info('Saving model...')
                    logging.info("Iteration completed: %d, remained %d" % (num_iters, args.iterations - num_iters))
                    logging.info('\n------------------------------------------------------')

                    if args.save_all:
                        save_orderdict = AdaVAE.state_dict()
                    else:
                        save_orderdict = collections.OrderedDict()
                        for name, parameter in AdaVAE.named_parameters():
                            if parameter.requires_grad:
                                save_orderdict[name] = parameter
                    torch.save(save_orderdict,
                               os.path.join(save_folder, 'ckpt/model',
                                            'model_' + '{:07d}'.format(num_iters) + '.pt'))
                if et >= args.early_stop:
                    logging.info("Early Stopping..")
                    break

        if not end:
            e += 1
            logging.info("Training loop. The ith epoch completed: %d" % e)

    ## last iteration testing
    logging.info("\n------------------------------------------------------")
    logging.info("test set")
    _ = val_step(test_loader)
    logging.info("validation set")
    _ = val_step(val_loader)
    logging.info("\n-------------------BEST RESULTS-----------------------")
    logging.info('Best nll      : %.4f' % best_val_nll)
    logging.info('Best loss     : %.4f' % best_val_loss_bpe)
    logging.info('Best elbo     : %.4f' % best_val_elbo)
    logging.info('Best ppl_bpe  : %.4f' % best_val_ppl_bpe)
    logging.info('Best ppl_elbo : %.4f' % best_val_ppl_elbo)
    logging.info('Best ppl_word : %.4f' % best_val_ppl_word)
    logging.info('Best reg_loss : %.4f' % best_val_reg)
    logging.info('Best MI       : %.4f' % best_val_mi)
    logging.info('Best AU       : %.4f' % best_val_n_au)

    if args.save_all:
        save_orderdict = AdaVAE.state_dict()
    else:
        save_orderdict = collections.OrderedDict()
        for name, parameter in AdaVAE.named_parameters():
            if parameter.requires_grad:
                save_orderdict[name] = parameter
    torch.save(save_orderdict, os.path.join(save_folder, 'model_latest.pt'))

    logging.info('Training complete.')

if __name__=="__main__":
    args = parser.parse_args()
    # args = parser.parse_args('--batch-sizes 100 --dataset penn_data --max_length 32 --pre_enc_iter start --add_attn --beta_0 1 --fb 1 --adapter_size 128 --iterations 200 --weighted_sample --latent_size 32 --encoder_n_layer 8 --decoder_n_layer 12 --adapter_init bert --attn_mode none --kl_rate 0.05'.split())
    # args = parser.parse_args('--batch-sizes 128 --max_length 25 --add_attn --adapter_size 128 --latent_size 32 '
    #                          '--decoder_n_layer 12 --encoder_n_layer 8 --adapter_init bert --attn_mode none --kl_rate 0.5'.split())
    train(args)