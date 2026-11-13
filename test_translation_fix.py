#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""复现并验证 HY-MT1.5-7B 翻译修复：完整链路单次翻译测试"""

import sys
import traceback

sys.path.insert(0, '.')

import torch
from transformers import AutoTokenizer, AutoModelForCausalLM

MODEL_NAME = 'tencent/HY-MT1.5-7B'
TEXT = '深圳還是會繼續封著'

print(f'加载 tokenizer: {MODEL_NAME}')
tok = AutoTokenizer.from_pretrained(MODEL_NAME, trust_remote_code=True)
print('chat_template 存在:', tok.chat_template is not None)
print('加载模型 (fp16, cpu)，首次约需1分钟...')
model = AutoModelForCausalLM.from_pretrained(
    MODEL_NAME,
    torch_dtype=torch.float16,
    device_map='cpu',
    trust_remote_code=True
)
print('模型加载完成\n')

messages = [{'role': 'user', 'content': f'将下面的文本翻译成英语，不要额外解释。{TEXT}'}]

try:
    # return_dict=True 统一返回 BatchEncoding，跨版本行为一致
    inputs = tok.apply_chat_template(
        messages,
        add_generation_prompt=True,
        tokenize=True,
        return_tensors='pt',
        return_dict=True
    ).to(model.device)
    print('inputs 类型:', type(inputs).__name__)
    print('渲染后的提示词:', repr(tok.decode(inputs['input_ids'][0])))

    outputs = model.generate(
        **inputs,
        max_new_tokens=100,
        do_sample=False,
        pad_token_id=tok.pad_token_id,
        eos_token_id=tok.eos_token_id
    )

    in_len = inputs['input_ids'].shape[-1]
    print(f'\n输入长度: {in_len}, 输出总长: {outputs.shape[-1]}')
    generated = outputs[0][in_len:]
    result = tok.decode(generated, skip_special_tokens=True)
    print(f'\n=== 翻译结果 ===\n{TEXT} -> {result!r}')
except Exception:
    traceback.print_exc()
    e = sys.exc_info()[1]
    print(f'\nrepr(e) = {e!r}')
    sys.exit(1)
