import os
from huggingface_hub import InferenceClient

# Set HF_TOKEN in the environment before running this script.
HF_TOKEN = os.environ.get("HF_TOKEN")
if not HF_TOKEN:
    raise RuntimeError("Set the HF_TOKEN environment variable before running this script.")

client = InferenceClient(token=HF_TOKEN, timeout=60)

# Для каждой модели явно указываем поддерживаемого бесплатного/партнёрского провайдера
test_configs = [
    {"model": "meta-llama/Llama-3.2-1B-Instruct", "provider": "together"},
    {"model": "meta-llama/Llama-3.2-3B-Instruct", "provider": "together"},
    {"model": "Qwen/Qwen2.5-7B-Instruct", "provider": "together"},
    {"model": "mistralai/Mistral-7B-Instruct-v0.3", "provider": "together"}
]

print("Проверяем доступность моделей...\n")

for item in test_configs:
    model_id = item["model"]
    prov = item["provider"]
    try:
        # Вызываем через OpenAI-совместимый Chat Completion API
        response = client.chat.completions.create(
            model=model_id,
            messages=[{"role": "user", "content": "Say 'OK' if online."}],
            max_tokens=10,
            provider=prov
        )
        reply = response.choices[0].message.content.strip()
        print(f"✅ Успех ({model_id} via {prov}): {reply}")
    except Exception as e:
        print(f"❌ Ошибка ({model_id}): {e}\n")