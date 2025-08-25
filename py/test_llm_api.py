# Please install OpenAI SDK first: `pip3 install openai`
# `python ~/test_llm_api.py --base_url .. --model ..`
import argparse
import os
import sys
from openai import OpenAI

def main(args):
    try:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            print("Please `export OPENAI_API_KEY=...` first")
            return 1
    except Exception as e:
        print(f"Error getting API key - {e}")
        return 1
    
    try:
        client = OpenAI(api_key=api_key, base_url=args.base_url)
        response = client.chat.completions.create(
            model=args.model,
            messages=[
                {"role": "system", "content": "You are a helpful assistant"},
                {"role": "user", "content": "Hello"},
            ],
            stream=False
        )

        print(response.choices[0].message.content)
    
    except Exception as e:
        print(f"Error during API call - {e}")
        return 1
    
    return 0

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test LLM API")
    parser.add_argument("--base_url", type=str, required=True, help="Base URL for the API")
    parser.add_argument("--model", type=str, required=True, help="Model to use for the chat completion")
    args = parser.parse_args()
    sys.exit(main(args))