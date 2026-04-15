import argparse
import base64
import csv
import io
import json
import time
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

import requests


def parse_decimal(value):
    text = str(value or "").strip()
    if not text:
        return Decimal("0")
    return Decimal(text)


def csv_date_to_iso(date_text):
    return datetime.strptime(date_text, "%m/%d/%Y").strftime("%Y-%m-%d")


def chunked(items, size):
    for index in range(0, len(items), size):
        yield items[index:index + size]


class GitHubReader:
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

    def read_text(self, file_path):
        data = self._api("GET", f"/contents/{file_path}", params={"ref": self.branch})
        content = data.get("content", "")
        return base64.b64decode(content).decode("utf-8")

    def read_json(self, file_path):
        return json.loads(self.read_text(file_path))

    def list_dir(self, dir_path):
        return self._api("GET", f"/contents/{dir_path}", params={"ref": self.branch})


class YNABClient:
    def __init__(self, token):
        self.base_url = "https://api.ynab.com/v1"
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _request(self, method, path, **kwargs):
        url = f"{self.base_url}{path}"

        for attempt in range(3):
            response = requests.request(
                method,
                url,
                headers=self.headers,
                timeout=60,
                **kwargs,
            )

            if response.status_code == 401:
                raise requests.HTTPError("YNAB API 返回 401，请检查 YNAB Token。", response=response)

            if response.status_code == 429:
                if attempt == 2:
                    response.raise_for_status()
                wait_seconds = 2 ** attempt
                print(f"YNAB API 限流，{wait_seconds} 秒后重试：{path}")
                time.sleep(wait_seconds)
                continue

            if response.status_code == 422:
                raise requests.HTTPError(f"YNAB API 返回 422：{response.text}", response=response)

            response.raise_for_status()

            if response.content:
                return response.json()
            return {}

        raise RuntimeError(f"请求失败：{path}")

    def get_budget(self, budget_id):
        return self._request("GET", f"/budgets/{budget_id}")

    def create_transactions(self, budget_id, transactions):
        return self._request(
            "POST",
            f"/budgets/{budget_id}/transactions",
            json={"transactions": transactions},
        )


def pick_backup_date(github_reader, budget_id, requested_date):
    if requested_date:
        return requested_date

    entries = github_reader.list_dir(f"budgets/{budget_id}/full")
    dates = sorted(
        [
            entry["name"][:-5]
            for entry in entries
            if entry.get("type") == "file" and entry.get("name", "").endswith(".json")
        ]
    )
    if not dates:
        raise SystemExit("未找到可恢复的备份文件。")
    return dates[-1]


def extract_budget(full_response):
    return full_response.get("data", {}).get("budget", {})


def print_accounts(backup_budget):
    print("账户列表（请先在 YNAB 中手动创建同名账户）：")
    for account in backup_budget.get("accounts", []):
        name = account.get("name") or ""
        account_type = account.get("type") or ""
        closed_text = "，已关闭" if account.get("closed") else ""
        print(f"  - {name} [{account_type}]{closed_text}")
    print("")


def print_categories(backup_budget):
    print("分类列表（分类与预算目标无法通过 API 自动恢复，请手动重建）：")
    category_groups = backup_budget.get("category_groups", [])

    if category_groups:
        for group in category_groups:
            if group.get("deleted"):
                continue
            group_name = group.get("name") or ""
            for category in group.get("categories", []):
                if category.get("deleted"):
                    continue
                category_name = category.get("name") or ""
                print(f"  - {group_name} / {category_name}")
    else:
        for category in backup_budget.get("categories", []):
            if category.get("deleted"):
                continue
            group_name = category.get("category_group_name") or ""
            category_name = category.get("name") or ""
            print(f"  - {group_name} / {category_name}")
    print("")


def build_account_map(target_budget):
    mapping = {}
    for account in target_budget.get("accounts", []):
        if account.get("deleted"):
            continue
        name = account.get("name")
        account_id = account.get("id")
        if name and account_id:
            mapping[name] = account_id
    return mapping


def build_category_maps(target_budget):
    combined_map = {}
    name_candidates = {}

    for group in target_budget.get("category_groups", []):
        if group.get("deleted"):
            continue
        group_name = group.get("name") or ""
        for category in group.get("categories", []):
            if category.get("deleted"):
                continue
            category_name = category.get("name") or ""
            category_id = category.get("id")
            if not category_name or not category_id:
                continue

            combined_key = f"{group_name}/{category_name}"
            combined_map[combined_key] = category_id
            name_candidates.setdefault(category_name, []).append(category_id)

    simple_map = {}
    for category_name, ids in name_candidates.items():
        if len(ids) == 1:
            simple_map[category_name] = ids[0]

    return combined_map, simple_map


def resolve_category_id(row, combined_map, simple_map):
    combined_name = str(row.get("Category Group/Category") or "").strip()
    category_name = str(row.get("Category") or "").strip()

    if combined_name and combined_name in combined_map:
        return combined_map[combined_name]

    if category_name and category_name in simple_map:
        return simple_map[category_name]

    return None


def csv_rows_to_transactions(csv_text, original_transactions, target_budget, backup_date):
    account_map = build_account_map(target_budget)
    combined_category_map, simple_category_map = build_category_maps(target_budget)

    rows = list(csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff"))))

    if len(rows) != len(original_transactions):
        print(
            f"提示：CSV 行数与原始交易数不一致，CSV={len(rows)}，"
            f"原始交易={len(original_transactions)}。将按行序继续处理。"
        )

    payloads = []

    for index, row in enumerate(rows):
        account_name = str(row.get("Account") or "").strip()
        if not account_name:
            print(f"跳过第 {index + 1} 行：缺少账户名。")
            continue

        account_id = account_map.get(account_name)
        if not account_id:
            print(f"跳过第 {index + 1} 行：目标预算中不存在账户 "{account_name}"。")
            continue

        outflow = parse_decimal(row.get("Outflow"))
        inflow = parse_decimal(row.get("Inflow"))
        amount = inflow - outflow
        milliunits = int(
            (amount * Decimal("1000")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )

        transaction = {
            "account_id": account_id,
            "date": csv_date_to_iso(str(row.get("Date") or "").strip()),
            "amount": milliunits,
            "cleared": {
                "Cleared": "cleared",
                "Uncleared": "uncleared",
                "Reconciled": "reconciled",
            }.get(str(row.get("Cleared") or "").strip(), "uncleared"),
        }

        payee_name = str(row.get("Payee") or "").strip()
        if payee_name:
            transaction["payee_name"] = payee_name

        memo = str(row.get("Memo") or "").strip()
        if memo:
            transaction["memo"] = memo

        flag = str(row.get("Flag") or "").strip().lower()
        if flag in {"red", "orange", "yellow", "green", "blue", "purple"}:
            transaction["flag_color"] = flag

        category_id = resolve_category_id(row, combined_category_map, simple_category_map)
        if category_id:
            transaction["category_id"] = category_id
        else:
            category_label = str(row.get("Category Group/Category") or "").strip()
            if category_label:
                print(f"提示：未找到分类 "{category_label}" 的映射，将以未分类导入。")

        original_id = ""
        if index < len(original_transactions):
            original_id = str(original_transactions[index].get("id") or "").strip()

        id_prefix = original_id[:8] if original_id else f"row{index + 1:05d}"
        transaction["import_id"] = f"RESTORE:{backup_date}:{id_prefix}"

        payloads.append(transaction)

    return payloads


def main():
    parser = argparse.ArgumentParser(description="从 GitHub 私有备份库恢复 YNAB 数据")
    parser.add_argument("--data-repo", required=True, help="私有库名称，格式 owner/repo")
    parser.add_argument("--github-token", required=True, help="GitHub PAT")
    parser.add_argument("--ynab-token", required=True, help="YNAB Personal Access Token")
    parser.add_argument("--budget-id", required=True, help="要恢复到的目标预算 ID")
    parser.add_argument("--date", help="要恢复的备份日期，格式 YYYY-MM-DD，默认最新")
    parser.add_argument("--dry-run", action="store_true", help="只显示将执行的操作，不实际导入")
    args = parser.parse_args()

    github = GitHubReader(args.github_token, args.data_repo, branch="main")
    ynab = YNABClient(args.ynab_token)

    backup_date = pick_backup_date(github, args.budget_id, args.date)
    print(f"将恢复备份日期：{backup_date}")
    print("")

    full_response = github.read_json(f"budgets/{args.budget_id}/full/{backup_date}.json")
    backup_budget = extract_budget(full_response)

    print_accounts(backup_budget)
    print_categories(backup_budget)

    csv_text = github.read_text(f"budgets/{args.budget_id}/transactions_csv/{backup_date}.csv")
    original_transactions = backup_budget.get("transactions", [])

    target_budget_response = ynab.get_budget(args.budget_id)
    target_budget = extract_budget(target_budget_response)

    transactions = csv_rows_to_transactions(
        csv_text,
        original_transactions,
        target_budget,
        backup_date,
    )

    if args.dry_run:
        print(f"Dry run：将导入 {len(transactions)} 条交易。")
        return

    if not transactions:
        print("没有可导入的交易。")
        return

    total = 0
    for batch_index, batch in enumerate(chunked(transactions, 50), start=1):
        ynab.create_transactions(args.budget_id, batch)
        total += len(batch)
        print(f"已导入第 {batch_index} 批，共 {len(batch)} 条交易。")

    print(f"恢复完成，共导入 {total} 条交易。")


if __name__ == "__main__":
    main()
