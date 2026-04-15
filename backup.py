import base64
import csv
import io
import json
import os
import time
from datetime import datetime

import requests


def parse_bool(value):
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def format_amount_from_milliunits(milliunits):
    amount = abs(milliunits) / 1000
    text = f"{amount:.3f}".rstrip("0").rstrip(".")
    if not text:
        return "0"
    return text


def format_csv_date(date_text):
    return datetime.strptime(date_text, "%Y-%m-%d").strftime("%m/%d/%Y")


def format_cleared_status(status):
    mapping = {
        "cleared": "Cleared",
        "uncleared": "Uncleared",
        "reconciled": "Reconciled",
    }
    return mapping.get(str(status).strip().lower(), "")


class YNABClient:
    def __init__(self, token):
        self.base_url = "https://api.ynab.com/v1"
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }

    def get(self, path, params=None):
        url = f"{self.base_url}{path}"
        for attempt in range(3):
            response = requests.get(url, headers=self.headers, params=params, timeout=60)

            if response.status_code == 401:
                raise requests.HTTPError("YNAB API 返回 401，请检查 YNAB_TOKEN。", response=response)

            if response.status_code == 429:
                if attempt == 2:
                    response.raise_for_status()
                wait_seconds = 2 ** attempt
                print(f"YNAB API 限流，{wait_seconds} 秒后重试：{path}")
                time.sleep(wait_seconds)
                continue

            response.raise_for_status()
            return response.json()

        raise RuntimeError(f"请求失败：{path}")

    def get_budgets(self):
        data = self.get("/budgets")
        return data.get("data", {}).get("budgets", [])

    def get_full_budget(self, budget_id, last_knowledge=None):
        params = None
        if last_knowledge is not None:
            params = {"last_knowledge_of_server": last_knowledge}
        return self.get(f"/budgets/{budget_id}", params=params)


class GitHubWriter:
    def __init__(self, token, repo, branch="main"):
        self.token = token
        self.repo = repo
        self.branch = branch
        self.base_url = f"https://api.github.com/repos/{repo}"

    def _api(self, method, path, **kwargs):
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        extra_headers = kwargs.pop("headers", {})
        headers.update(extra_headers)

        response = requests.request(
            method,
            f"{self.base_url}{path}",
            headers=headers,
            timeout=60,
            **kwargs,
        )

        if response.status_code == 422:
            raise requests.HTTPError(f"GitHub API 返回 422：{response.text}", response=response)

        response.raise_for_status()

        if response.content:
            return response.json()
        return {}

    def read_json(self, file_path):
        try:
            data = self._api("GET", f"/contents/{file_path}", params={"ref": self.branch})
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                return None
            raise

        content = data.get("content", "")
        decoded = base64.b64decode(content).decode("utf-8")
        return json.loads(decoded)

    def write_file(self, file_path, content_dict, commit_message):
        text_content = f"{json.dumps(content_dict, ensure_ascii=False, indent=2)}\n"
        self.write_text(file_path, text_content, commit_message)

    def write_text(self, file_path, text_content, commit_message):
        sha = None
        try:
            existing = self._api("GET", f"/contents/{file_path}", params={"ref": self.branch})
            sha = existing.get("sha")
        except requests.HTTPError as exc:
            if exc.response is None or exc.response.status_code != 404:
                raise

        encoded_content = base64.b64encode(text_content.encode("utf-8")).decode("utf-8")
        payload = {
            "message": commit_message,
            "content": encoded_content,
            "branch": self.branch,
        }
        if sha:
            payload["sha"] = sha

        self._api("PUT", f"/contents/{file_path}", json=payload)


def transactions_to_csv(transactions):
    columns = [
        "Account",
        "Flag",
        "Date",
        "Payee",
        "Category Group/Category",
        "Category Group",
        "Category",
        "Memo",
        "Outflow",
        "Inflow",
        "Cleared",
    ]

    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator="\n")
    writer.writeheader()

    for transaction in transactions:
        amount = transaction.get("amount", 0)
        category_group = transaction.get("category_group_name") or ""
        category = transaction.get("category_name") or ""
        combined_category = ""
        if category_group and category:
            combined_category = f"{category_group}/{category}"
        elif category:
            combined_category = category

        row = {
            "Account": transaction.get("account_name") or "",
            "Flag": transaction.get("flag_color") or "",
            "Date": format_csv_date(transaction.get("date", "1970-01-01")),
            "Payee": transaction.get("payee_name") or "",
            "Category Group/Category": combined_category,
            "Category Group": category_group,
            "Category": category,
            "Memo": transaction.get("memo") or "",
            "Outflow": format_amount_from_milliunits(amount) if amount < 0 else "",
            "Inflow": format_amount_from_milliunits(amount) if amount > 0 else "",
            "Cleared": format_cleared_status(transaction.get("cleared")),
        }
        writer.writerow(row)

    return f"\ufeff{buffer.getvalue()}"


def main():
    ynab_token = os.getenv("YNAB_TOKEN")
    github_pat = os.getenv("GITHUB_PAT")
    data_repo = os.getenv("DATA_REPO")
    data_repo_branch = os.getenv("DATA_REPO_BRANCH", "main")
    full_backup = parse_bool(os.getenv("FULL_BACKUP", "false"))
    today = datetime.utcnow().strftime("%Y-%m-%d")

    missing = [
        name
        for name, value in {
            "YNAB_TOKEN": ynab_token,
            "GITHUB_PAT": github_pat,
            "DATA_REPO": data_repo,
        }.items()
        if not value
    ]
    if missing:
        raise SystemExit(f"缺少环境变量：{', '.join(missing)}")

    ynab = YNABClient(ynab_token)
    github = GitHubWriter(github_pat, data_repo, data_repo_branch)

    print(f"开始备份，日期：{today}，FULL_BACKUP={full_backup}")

    last_run = github.read_json("last_run.json") or {}
    previous_budgets_state = last_run.get("budgets", {})

    budgets = ynab.get_budgets()
    github.write_file(
        "budgets/index.json",
        {
            "updated_at": today,
            "budgets": budgets,
        },
        f"备份 budgets/index.json {today}",
    )

    new_state = {
        "updated_at": today,
        "budgets": {},
    }

    for budget in budgets:
        budget_id = budget.get("id")
        budget_name = budget.get("name") or budget_id

        if not budget_id:
            print("发现缺少 id 的预算，已跳过。")
            continue

        try:
            last_knowledge = None
            if not full_backup:
                last_knowledge = (
                    previous_budgets_state.get(budget_id, {}).get("server_knowledge")
                )

            print(f"开始处理预算：{budget_name} ({budget_id})")
            full_response = ynab.get_full_budget(budget_id, last_knowledge=last_knowledge)

            github.write_file(
                f"budgets/{budget_id}/full/{today}.json",
                full_response,
                f"备份预算 {budget_name} 完整数据 {today}",
            )

            transactions = (
                full_response.get("data", {})
                .get("budget", {})
                .get("transactions", [])
            )

            if transactions:
                csv_content = transactions_to_csv(transactions)
                github.write_text(
                    f"budgets/{budget_id}/transactions_csv/{today}.csv",
                    csv_content,
                    f"备份预算 {budget_name} 交易 CSV {today}",
                )

            server_knowledge = full_response.get("data", {}).get("server_knowledge")
            if server_knowledge is None:
                server_knowledge = budget.get("server_knowledge")
            if server_knowledge is None:
                server_knowledge = previous_budgets_state.get(budget_id, {}).get("server_knowledge")

            new_state["budgets"][budget_id] = {
                "name": budget_name,
                "server_knowledge": server_knowledge,
            }

            print(
                f"预算备份完成：{budget_name} ({budget_id})，"
                f"交易数：{len(transactions)}，server_knowledge={server_knowledge}"
            )
        except Exception as exc:
            print(f"预算备份失败：{budget_name} ({budget_id})，错误：{exc}")
            if budget_id in previous_budgets_state:
                new_state["budgets"][budget_id] = previous_budgets_state[budget_id]
            continue

    github.write_file(
        "last_run.json",
        new_state,
        f"更新 last_run.json {today}",
    )

    print("全部备份流程结束。")


if __name__ == "__main__":
    main()
