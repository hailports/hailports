from core.free_llm_pool import try_free_providers
print(f"Free provider used: {try_free_providers(prompt, system=None, max_tokens=2048)}")
