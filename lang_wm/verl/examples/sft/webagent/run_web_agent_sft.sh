 torchrun -m --nnodes 1 \
 --nproc_per_node=8 \
verl.trainer.fsdp_sft_trainer \
data.train_files=SFT_data/train.parquet \
data.val_files=SFT_data/test.parquet \
data.prompt_key=question \
data.response_key=answer \
data.micro_batch_size_per_gpu=1 \
model.partial_pretrain=DeepSeek-R1-Distill-Qwen-1.5B \
model.lora_rank=32 \
model.lora_alpha=16 \
trainer.default_hdfs_dir=hdfs://user/verl/experiments/webagent/DeepSeek-R1-Distill-Qwen-1.5B \
trainer.project_name=webagent-sft \
trainer.experiment_name=webagent-sft-DeepSeek-R1-Distill-Qwen-1.5B \
trainer.total_epochs=40 \
trainer.logger="['console','wandb']" \
data.train_batch_size=8 \
trainer.default_local_dir=log/webagent-sft-DeepSeek-R1-Distill-Qwen-1.5B