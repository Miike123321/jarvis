from gradio_client import Client

# Подключаемся к этому конкретному Space
client = Client("gimbx/Uncensored-HackerCoding-GPT")

# Отправляем промпт
result = client.predict(
    prompt="can you write a simple python code to print hello world?",
    api_name="/predict"
)

print(result)