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

def get_token_with_type():
    if (os.environ.get("GITHUB_APP_ID") is not None and
        os.environ.get("GITHUB_APP_INSTALLATION_ID") is not None and
        os.environ.get("GITHUB_APP_PRIVATE_KEY") is not None):
        GITHUB_APP_ID = os.environ.get("GITHUB_APP_ID")
        PRIVATE_KEY = os.environ.get("GITHUB_APP_PRIVATE_KEY")
        INSTALLATION_ID = os.environ.get("GITHUB_APP_INSTALLATION_ID")

        with open(PRIVATE_KEY_PATH, "rb") as key_file:
            PRIVATE_KEY = key_file.read()

        jwt_token = create_jwt(GITHUB_APP_ID, PRIVATE_KEY)
        integration = GithubIntegration(integration_id=GITHUB_APP_ID, private_key=PRIVATE_KEY, base_url=base_url())
        install_token = integration.get_access_token(INSTALLATION_ID).token
        return ["Bearer", install_token]
    return ["token", os.environ.get("GITHUB_TOKEN")]

app = Flask(__name__)
webhook = Webhook(app, endpoint="/", secret=os.environ.get("GITHUB_WEBHOOK_SECRET"))

@app.route("/")
def ok():
    return "ok"

@webhook.hook("issue_comment")
def on_issue_comment(data):
    token_type, token = get_token_with_type()
    precontext = ""
    content = ""
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

        context = '''
        ## 入力仕様
        - {input_text}

        ## 指示
        - すべて日本語で回答してください。
        - 入力の内容が文章の場合は推敲して下さい。
        - 入力の内容がプログラムコードの場合はリファクタリングしてください。
        - あなたが推敲した、もしくはリファクタリングした内容をdiff形式で回答してください。また何を変更したかも説明してください。
        - あなたが推敲する必要がない、リファクタリングする必要がない場合は、推薦の必要がないと述べたあとに、入力内容を解説してください。。

        ## 入力
        {content}
        '''

        client = Github(login_or_token=token, base_url=base_url())
        repo = client.get_repo(data['repository']['full_name'])
        issue = ""
        input_text = ""
        if "@gpt-bot /comment" in data['comment']['body']:
            input_text = 'GitHubで生成されたIssueのコメントを入力します。'
            content = data['comment']['body'].replace("@gpt-bot /comment", "")
            issue = repo.get_issue((data['issue']['number']))
        elif "@gpt-bot /pr" in data['comment']['body'] and data['issue']['pull_request'] is not None:
            input_text = 'GitHubで生成されたPull Requestのdiffを入力します'

            headers = {
                "Authorization": f"{token_type} {token}",
                "Accept": "application/vnd.github.v3.diff"
            }

            diff = requests.get(data['issue']['pull_request']['url'], headers=headers)
            diff.raise_for_status()
            content = diff.text

            pull_request = repo.get_pull(data['issue']['number'])
            issue = pull_request.as_issue()
        else:
            return

        if(len(content)> 8000):
            content = content[:8000]
            issue.create_comment("コンテンツが長すぎるので、コンテンツを切り詰めました")

        print("send request to openapi")
        response = openai.ChatCompletion.create(
            model="gpt-4",
            messages=[
                {"role": "user", "content": precontext},
                {"role": "user", "content": textwrap.dedent(context).format(content=content, input_text=input_text)},
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
    app.run(host="127.0.0.1", port=10080)

