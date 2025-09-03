# Please install OpenAI SDK first: `pip3 install openai`
# `python ~/my/test_llm_api.py <base url> <model>`
import argparse
import json
import os
import sys
import time
from dotenv import load_dotenv
from openai import OpenAI
from pathlib import Path

def load_api_key(key_name: str, env_file: str=None) -> str:
    if env_file is not None:
        if not os.path.exists(env_file):
            print(f"Env file {env_file} does not exist.")
            return None
        
        load_dotenv(dotenv_path=env_file)
    return os.getenv(key_name)

def main(args):
    key_name = args.key or "API_KEY"
    env_file = Path(args.env) if args.env is not None else (
               Path.home() / "my" / "_env" / "test.env")
    
    try:
        api_key = load_api_key(key_name, env_file)
        if not api_key:
            print(f"API key '{key_name}' does not exist. Please set it by \n"
                  f"  1. Run `export {key_name}=..` in terminal \n"
                  f"  2. Create a .env file with `{key_name}=..`")
            return 1
    except Exception as e:
        print(f"Error getting API key - {e}")
        return 1
    
    try:
        client = OpenAI(api_key=api_key, base_url=args.base_url)
        
        start_time = time.time()
        response = client.chat.completions.create(
            model=args.model,
            messages=[
                {"role": "system", "content": "You are a helpful assistant"},
                {"role": "user", "content": "Hello! What's your name?"},
            ],
            stream=False
        )
        print(f"API call took {time.time() - start_time:.2f} seconds: \n")

        #print(response.choices[0].message.content)
        print(json.dumps(response.model_dump(), indent=4, ensure_ascii=False))
    
    except Exception as e:
        print(f"Error during API call - {e}")
        return 1
    
    return 0

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test LLM API")
    parser.add_argument("base_url", type=str, help="Base URL for the API")
    parser.add_argument("model", type=str, help="Model to use for the chat completion")
    parser.add_argument("--key", type=str, default=None, help="Environment variable name for the API key")
    parser.add_argument("--env", type=str, default=None, help="Path to .env file containing the API key")
    args = parser.parse_args()
    sys.exit(main(args))