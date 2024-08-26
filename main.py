import logging
import os
import re
import textwrap

import requests
import tiktoken
from flask import Flask, jsonify
from github import Github, GithubIntegration
from github_webhook import Webhook
from openai import OpenAI


def get_github_client(token):
    return Github(login_or_token=token, base_url=base_url())


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


def query_ai(content):
    client = OpenAI()
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {
                "role": "user",
                "content": content,
            },
        ],
    )
    return response.choices[0].message.content.strip()


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


def process_issue_comment(data):
    org = data["repository"]["owner"]["login"]
    repo_name = data["repository"]["name"]
    _, token = get_token_with_type(org, repo_name)
    client = get_github_client(token)
    repo = client.get_repo(data["repository"]["full_name"])
    issue = repo.get_issue(data["issue"]["number"])

    if re.match(r"@gpt-bot", data["comment"]["body"]):
        app.logger.info(
            "repo: %s issue_no: %s command: %s",
            data["repository"]["full_name"],
            data["issue"]["number"],
            data["comment"]["body"],
        )

    if re.match(r"@gpt-bot\s*今北産業", data["comment"]["body"]):
        handle_summary_request(issue)
    elif re.match(r"@gpt-bot\s*/comment", data["comment"]["body"]):
        handle_comment_request(data, issue)
    elif (
        "pull_request" in data["issue"]
        and data["issue"]["pull_request"] is not None
        and re.match(r"@gpt-bot", data["comment"]["body"])
    ):
        handle_pull_request(data, issue)
    else:
        return jsonify({"message": "Unsupported command"}), 400


def handle_summary_request(issue):
    body = issue.body
    if "<!-- summary start -->" in body:
        body = body.split("<!-- summary start -->")[0]

    comments = issue.get_comments()
    user_comments = "\n".join(
        f"- {comment.user.login}: {comment.body}"
        for comment in comments
        if "@gpt-bot" not in comment.body
    )

    context = """
    ## 入力仕様
    - GitHub Issue でおこなわれている議論の内容をお渡しします。

    ## 指示
    - すべて日本語で回答してください。
    - 先頭に `### AIによるサマリー` というヘッダを付けてください。
    - 議論のサマリーを作成してください
    - 議論に途中から参加する人がひと目で全容がわかる内容にしてください
    - 重要なことはもれなく含めてください。特に期限系のものは必ず含めてください
    - 冒頭に`3行まとめ`を記述したあとに、詳細なサマリを記述してください
    ## 入力
    ### 本文
    {body}
    ### コメント
    {comments}
    """
    summary = query_ai(
        textwrap.dedent(context).format(body=body, comments=user_comments)
    )
    update_issue_body(issue, summary)
    issue.create_comment("AIによる議論のサマリがIssueの本文に更新されました。")


def handle_comment_request(data, issue):
    body = data["comment"]["body"].replace("@gpt-bot /comment", "").strip()
    context = """
    ## 入力仕様
    - GitHubで生成されたIssueのコメントを入力します。

    ## 指示
    - すべて日本語で回答してください。
    - 入力の内容が文章の場合は推敲してください。
    - 入力の内容がプログラムコードの場合はリファクタリングしてください。また脆弱性やバグを修正してください。
    - あなたが推敲した、もしくはリファクタリングした内容をdiff形式で回答してください。また何を変更したかも説明してください。
    - あなたが推敲する必要がない、リファクタリングする必要がない場合は、推薦の必要がないと述べたあとに、入力内容を解説してください。
    ## 入力
    {body}
    """
    response = query_ai(textwrap.dedent(context).format(body=body))
    issue.create_comment(response)


def handle_pull_request(data, issue):
    token_type, token = get_token_with_type(
        data["repository"]["owner"]["login"], data["repository"]["name"]
    )
    headers = {
        "Authorization": f"{token_type} {token}",
        "Accept": "application/vnd.github.v3.diff",
    }

    url = data["issue"]["pull_request"]["url"] + "/files"
    response = requests.get(url, headers=headers)
    response.raise_for_status()

    diff = response.json()
    diff = "\n".join(
        f"### {item.get('filename')}\n```diff\n{item.get('patch')}\n```"
        for item in diff
        if item.get("filename") and item.get("patch")
    )

    if not diff:
        issue.create_comment("検出できる差分がありませんでした。")
        return

    encoding = tiktoken.encoding_for_model("gpt-4")
    if len(encoding.encode(diff)) > 128000:
        issue.create_comment(
            f"コンテンツが長すぎるので、処理できませんでした トークン数: {len(encoding.encode(diff))}"
        )
        return

    if "@gpt-bot /command" in data["comment"]["body"]:
        instructions = data["comment"]["body"].replace("@gpt-bot /command", "").strip()
        context = """
        ## 入力仕様
        - GitHubで生成されたPull Requestのdiffを入力します。"- " から始まる行は修正前のコンテンツに該当します。"+ " から始まる行は修正後のコンテンツに該当します。

        ## 指示
        {instructions}

        ## 入力
        {diff}
        """
        response = query_ai(context.format(instructions=instructions, diff=diff))
    else:
        context = """
        ## 入力仕様
        - GitHubで生成されたPull Requestのdiffを入力します。"- " から始まる行は修正前のコンテンツに該当します。"+ " から始まる行は修正後のコンテンツに該当します。

        ## 指示
        - すべて日本語で回答してください。
        - diffをレビューし、改善点があれば提案してください。
        - 改善点がない場合は、その旨を述べた後に解説してください。
        - 提案がある場合は、リファクタリングされたコードをdiff形式で提示してください。
        ## 入力
        {diff}
        """
        response = query_ai(textwrap.dedent(context).format(diff=diff))

    issue.create_comment(response)


app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
webhook = Webhook(app, endpoint="/", secret=os.environ.get("GITHUB_WEBHOOK_SECRET"))


@app.route("/")
def ok():
    return "ok"


@webhook.hook("issue_comment")
def on_issue_comment(data):
    process_issue_comment(data)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10080)
