from github_webhook import Webhook
from flask import Flask
from github import Github,GithubIntegration
import os
import pprint
import jwt
import datetime
import requests
import openai
import textwrap
import tiktoken

def create_jwt(app_id, private_key):
      now = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
      payload = {
        "iat": now,
        "exp": now + (10 * 60),  # max 10 minutes
        "iss": app_id,
      }

      token = jwt.encode(payload, private_key, algorithm="RS256")
      return token

def base_url():
    base_url = "https://api.github.com/"
    if os.environ.get("GITHUB_API") is not None:
        base_url = os.environ.get("GITHUB_API").rstrip("/")
    return base_url

def get_token_with_type(org, repo):
    if (os.environ.get("GITHUB_APP_ID") is not None and
        os.environ.get("GITHUB_APP_PRIVATE_KEY") is not None):
        GITHUB_APP_ID = os.environ.get("GITHUB_APP_ID")
        PRIVATE_KEY = os.environ.get("GITHUB_APP_PRIVATE_KEY")
        jwt_token = create_jwt(GITHUB_APP_ID, PRIVATE_KEY)
        integration = GithubIntegration(integration_id=GITHUB_APP_ID, private_key=PRIVATE_KEY, base_url=base_url())
        installation_id = integration.get_repo_installation(org, repo).id

        install_token = integration.get_access_token(installation_id).token
        return ["Bearer", install_token]
    return ["token", os.environ.get("GITHUB_TOKEN")]

app = Flask(__name__)
webhook = Webhook(app, endpoint="/", secret=os.environ.get("GITHUB_WEBHOOK_SECRET"))

@app.route("/")
def ok():
    return "ok"

@webhook.hook("issue_comment")
def on_issue_comment(data):
    query = ""
    if (
            "@gpt-bot" in data['comment']['body'] and
            (
                data['action'] == "created" or
                (
                    data['action'] == "edited" and
                    data['changes']['body']['from'] not in "@gpt-bot"
                )
            )
    ):
        print("GPT-BOTのコメントを検知しました。{}".format(data['comment']['body']))
        token_type, token = get_token_with_type(data['repository']['owner']['login'], data['repository']['name'])

        context = '''
        ## 入力仕様
        - {input_text}

        ## 指示
        - すべて日本語で回答してください。
        - 入力の内容が文章の場合は推敲して下さい。
        - 入力の内容がプログラムコードの場合はリファクタリングしてください。
        - あなたが推敲した、もしくはリファクタリングした内容をdiff形式で回答してください。また何を変更したかも説明してください。
        - あなたが推敲する必要がない、リファクタリングする必要がない場合は、推薦の必要がないと述べたあとに、入力内容を解説してください。。

        {instructions}
        ## 入力
        {query}
        '''

        client = Github(login_or_token=token, base_url=base_url())
        repo = client.get_repo(data['repository']['full_name'])
        issue = ""
        input_text = ""
        instructions = ""
        if "@gpt-bot /comment" in data['comment']['body']:
            input_text = 'GitHubで生成されたIssueのコメントを入力します。'
            query = data['comment']['body'].replace("@gpt-bot /comment", "")
            issue = repo.get_issue((data['issue']['number']))
        elif data['issue']['pull_request'] is not None:
            input_text = 'GitHubで生成されたPull Requestのdiffを入力します。"- " から始まる行は修正前のコンテンツに該当します。"+ " から始まる行は修正後のコンテンツに該当します'
            headers = {
                "Authorization": f"{token_type} {token}",
                "Accept": "application/vnd.github.v3.diff"
            }

            diff = requests.get(data['issue']['pull_request']['url'], headers=headers)
            diff.raise_for_status()
            query = diff.text

            pull_request = repo.get_pull(data['issue']['number'])
            issue = pull_request.as_issue()

            if "@gpt-bot /pr" in data['comment']['body']:
                pass
            elif "@gpt-bot /command" in data['comment']['body']:
                context = '''
                ## 入力仕様
                - {input_text}

                ## 指示
                {instructions}

                ## 入力
                {query}
                '''
                instructions = data['comment']['body'].replace("@gpt-bot /command", "")
            elif "@gpt-bot /unittest" in data['comment']['body']:
                context = '''
                ## 入力仕様
                - {input_text}

                ## 指示
                - すべて日本語で回答してください。
                - 入力の内容がプログラムコードの場合はユニットテストを実装してください。
                ## 入力
                {query}
                '''
        else:
            return

        encoding = tiktoken.encoding_for_model("gpt-4")
        if(len(encoding.encode(query))> 8000):
            issue.create_comment("コンテンツが長すぎるので、処理できませんでした")
            return

        print("send request to openapi")
        response = openai.ChatCompletion.create(
            model="gpt-4",
            messages=[
                {"role": "user", "content": textwrap.dedent(context).format(
                    query=query,
                    input_text=input_text,
                    instructions=instructions
                    )},
            ]
        )

        comment = textwrap.dedent('''
            <details>
            <summary>GPT-BOTからの推薦</summary>

            {suggest}

            </details>
        ''').format(suggest=response.choices[0]["message"]["content"].replace("@gpt-bot /comment", "").replace("@gpt-bot /pr", ""))
        print(response.choices[0]["message"]["content"])
        issue.create_comment(comment)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10080)

