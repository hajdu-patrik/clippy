# Local LLM

Qwen3-14B tuned for a 16 GB laptop GPU (RTX 4090 Laptop) with 32 GB RAM. This is the `ollama` mode, the default.
The other modes (OpenRouter, your Claude or ChatGPT subscription, none) are chosen in *Settings, AI* or with
`clippy llm <mode>` (see the README); any OpenAI-compatible API also works through `CLIPPY_LLM_BACKEND=openai`
(`.env.example`).

## Settings

| Setting | Value | Why |
|---|---|---|
| Quantisation | **Q6_K** (Qwen's official GGUF, `clippy-qwen3:14b-q6k`) | Near-lossless. Non-English text suffers more from 4-bit quantisation than English. Q8_0 (15.7 GB) would not fit beside the context. |
| Context | 16K tokens | An 8-minute transcript window plus reasoning and the prompt fit comfortably |
| KV cache | q8_0 with flash attention | Half the VRAM of fp16 at no quality cost; measured 13.3 → 12.2 GiB |
| Sampling | thinking: T 0.6 / top_p 0.95 / top_k 20; fast: T 0.7 / top_p 0.8 / top_k 20; min_p 0; presence_penalty 1.5 | Qwen3 model card (presence_penalty 1.5 for quantised models) |
| Thinking mode | on for clip selection and re-ranking, off for chat and hook shortening | Reasoning where the decision matters, speed where it does not |
| Output | JSON schema (structured outputs) | Always valid JSON with the expected fields |
| VRAM scheduling | the model is unloaded before Demucs, Whisper and the aligner | The 14B model takes ~14 GB; they would not fit beside it |

Measured on an RTX 4090 Laptop GPU: Q6_K ≈ 36 tokens/s (Q4_K_M ≈ 44) and prompt processing ≈ 2000 tokens/s; an 8-minute
window takes ≈ 35 s in thinking mode.

Ollama server settings (user environment variables; `clippy start` also passes them when it starts Ollama):
`OLLAMA_FLASH_ATTENTION=1`, `OLLAMA_KV_CACHE_TYPE=q8_0`, `OLLAMA_NUM_PARALLEL=1`, `OLLAMA_MAX_LOADED_MODELS=1`.
To create the model: `ollama pull hf.co/Qwen/Qwen3-14B-GGUF:Q6_K`, then
`ollama create clippy-qwen3:14b-q6k -f models/Modelfile.qwen3-14b-q6k`.
