import os
import textwrap

import requests
import tiktoken
from flask import Flask
from github import Github, GithubIntegration
from github_webhook import Webhook
from openai import OpenAI


def update_issue_body(issue, summary):
    body_lines = issue.body.splitlines()
    new_body = []
    summary_marker_start = "<!-- summary start -->"
    summary_marker_end = "<!-- summary end -->"
    in_summary = False

    for line in body_lines:
        if summary_marker_start in line:
            in_summary = True
            new_body.append(summary_marker_start)
            new_body.append("### AIによるサマリー")
            new_body.append(summary)
            continue
        if summary_marker_end in line:
            in_summary = False
            new_body.append(summary_marker_end)
            continue
        if not in_summary:
            new_body.append(line)

    if summary_marker_start not in new_body:
        new_body.append(summary_marker_start)
        new_body.append(summary)
        new_body.append(summary_marker_end)

    issue.edit(body="\n".join(new_body))


def generate_summary(context, body, comments):
    client = OpenAI()
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {
                "role": "user",
                "content": textwrap.dedent(context).format(
                    body=body, comments=comments
                ),
            },
        ],
    )
    return response.choices[0].message.content.strip()


def base_url():
    base_url = "https://api.github.com/"
    if os.environ.get("GITHUB_API") is not None:
        base_url = os.environ.get("GITHUB_API").rstrip("/")
    return base_url


def get_token_with_type(org, repo):
    if (
        os.environ.get("GITHUB_APP_ID") is not None
        and os.environ.get("GITHUB_APP_PRIVATE_KEY") is not None
    ):
        GITHUB_APP_ID = os.environ.get("GITHUB_APP_ID")
        PRIVATE_KEY = os.environ.get("GITHUB_APP_PRIVATE_KEY")
        integration = GithubIntegration(
            integration_id=GITHUB_APP_ID, private_key=PRIVATE_KEY, base_url=base_url()
        )
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
    if "@gpt-bot" in data["comment"]["body"] and (
        data["action"] == "created"
        or (
            data["action"] == "edited"
            and data["changes"]["body"]["from"] not in "@gpt-bot"
        )
    ):
        print("GPT-BOTのコメントを検知しました。{}".format(data["comment"]["body"]))
        token_type, token = get_token_with_type(
            data["repository"]["owner"]["login"], data["repository"]["name"]
        )

        context = """
        ## 入力仕様
        - {input_text}

        ## 指示
        - すべて日本語で回答してください。
        - 入力の内容が文章の場合は推敲して下さい。
        - 入力の内容がプログラムコードの場合はリファクタリングしてください。また脆弱性やバグを修正してください。
        - あなたが推敲した、もしくはリファクタリングした内容をdiff形式で回答してください。また何を変更したかも説明してください。
        - あなたが推敲する必要がない、リファクタリングする必要がない場合は、推薦の必要がないと述べたあとに、入力内容を解説してください。。

        {instructions}
        ## 入力
        {query}
        """

        client = Github(login_or_token=token, base_url=base_url())
        repo = client.get_repo(data["repository"]["full_name"])
        issue = ""
        input_text = ""
        instructions = ""

        if "@gpt-bot 今北産業" in data["comment"]["body"]:
            print("今北産業サマリ要求を検知しました。")
            token_type, token = get_token_with_type(
                data["repository"]["owner"]["login"], data["repository"]["name"]
            )
            client = Github(login_or_token=token, base_url=base_url())
            repo = client.get_repo(data["repository"]["full_name"])
            issue = repo.get_issue((data["issue"]["number"]))

            comments = issue.get_comments()
            context = """
            ## 入力仕様
            - GitHub Issue でおこなわれている議論の内容をお渡しします。

            ## 指示
            - すべて日本語で回答してください。
            - 議論のサマリーを作成してください
            - 議論に途中から参加する人がひと目で全容がわかる内容にしてください
            - 重要なことはもれなく含めてください
            ## 入力
            ### 本文
            {body}
            ### コメント
            {comments}
            """
            body = issue.body
            # サマリがあれば除外する
            if "<!-- summary start -->" in body:
                body = body.split("<!-- summary start -->")[0]

            user_comments = ""
            for comment in comments:
                if "@gpt-bot" in comment.body:
                    continue
                user_comments += f"- {comment.user.login}: {comment.body}\n"

            summary = generate_summary(context, body, user_comments)

            print("サマリを更新しています。")
            update_issue_body(issue, summary)
            issue.create_comment("議論のサマリがIssueの本文に更新されました。")
        elif "@gpt-bot /comment" in data["comment"]["body"]:
            input_text = "GitHubで生成されたIssueのコメントを入力します。"
            query = data["comment"]["body"].replace("@gpt-bot /comment", "")
            issue = repo.get_issue((data["issue"]["number"]))
        elif data["issue"]["pull_request"] is not None:
            input_text = 'GitHubで生成されたPull Requestのdiffを入力します。"- " から始まる行は修正前のコンテンツに該当します。"+ " から始まる行は修正後のコンテンツに該当します'
            headers = {
                "Authorization": f"{token_type} {token}",
                "Accept": "application/vnd.github.v3.diff",
            }

            url = data["issue"]["pull_request"]["url"] + "/files"
            response = requests.get(url, headers=headers)
            response.raise_for_status()

            diff = response.json()

            query = ""

            for item in diff:
                filename = item.get("filename")
                patch = item.get("patch")

                if filename is None or patch is None:
                    continue

                query += f"### {filename}\n"
                query += f"```diff\n{patch}\n```\n"

            pull_request = repo.get_pull(data["issue"]["number"])
            issue = pull_request.as_issue()

            if "@gpt-bot /pr" in data["comment"]["body"]:
                pass
            elif "@gpt-bot /command" in data["comment"]["body"]:
                context = """
                ## 入力仕様
                - {input_text}

                ## 指示
                - すべて日本語で回答してください。
                {instructions}

                ## 入力
                {query}
                """
                instructions = data["comment"]["body"].replace("@gpt-bot /command", "")
            elif "@gpt-bot /unittest" in data["comment"]["body"]:
                context = """
                ## 入力仕様
                - {input_text}

                ## 指示
                - すべて日本語で回答してください。
                - 入力の内容がプログラムコードの場合はユニットテストを実装してください。
                ## 入力
                {query}
                """
        else:
            return

        encoding = tiktoken.encoding_for_model("gpt-4")
        if len(encoding.encode(query)) > 128000:
            issue.create_comment(
                f"コンテンツが長すぎるので、処理できませんでした トークン数: {len(encoding.encode(query))}"
            )
            return

        if query == "":
            issue.create_comment("検出できる差分がありませんでした。")

        print("send request to openapi")
        client = OpenAI()
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "user",
                    "content": textwrap.dedent(context).format(
                        query=query, input_text=input_text, instructions=instructions
                    ),
                },
            ],
        )

        comment = textwrap.dedent(
            """
            ## GPT-BOTからの推薦

            {suggest}
        """
        ).format(
            suggest=response.choices[0]
            .message.content.replace("@gpt-bot /comment", "")
            .replace("@gpt-bot /pr", "")
        )
        print(response.choices[0].message.content)
        issue.create_comment(comment)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10080)
