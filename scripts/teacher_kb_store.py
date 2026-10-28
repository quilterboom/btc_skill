#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
teacher_kb_store.py
==================
IMA 当「数据库」：老师单分析结果的读写层。

liusir 10-28 决策：「你就把 ima 当做数据库去存数据就行」。

为什么是 KB 而不是 notes：
  - notes 只能 import_doc（新建）/ append_doc（追加），**没有覆盖/删除**，
    每次刷新都会往同一篇笔记后面追加新快照 → 笔记无限膨胀，且读的时候
    不知道哪一段是最新。
  - KB 的 add_knowledge 同样不可逆（推土机），但**每份文档是一个独立 media_id**，
    可以靠「文件名带时间戳」+ get_knowledge_list 排序取最新的一份，
    读路径干净、写入幂等（同名检测跳过）。

所以这里的模式是 **append-only 版本链**：
  写入 → 生成 `老师单分析_vN_YYYYMMDD_HHMM.md` 新文档
  读取 → get_knowledge_list 列出全部 → 按文件名里的版本号/时间戳取最新 → get_media_info → 抓 URL 正文
  绝不会删除任何旧文档（推土机纪律：只增不删）

正文格式：markdown 里嵌 ```json code block```（KB 不接受 .json 直传）。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from typing import Optional

CST = timezone(timedelta(hours=8))
SKILL_DIR = os.path.expanduser("~/.hermes/skills/ima-skill")
KB_NAME = "BTC 交易策略"

# 版本号前缀（用于在文件名里排序取最新）
DOC_PREFIX = "老师单分析_v"


# ---------------------------------------------------------------- 凭证 / API
def _opts() -> str:
    cli = open(os.path.expanduser("~/.config/ima/client_id")).read().strip()
    api = open(os.path.expanduser("~/.config/ima/api_key")).read().strip()
    return json.dumps({"clientId": cli, "apiKey": api})


def _api(api_path: str, body: dict) -> dict:
    """调 IMA API。失败即抛（不兜底、不静默）。"""
    cmd = ["node", os.path.join(SKILL_DIR, "ima_api.cjs"), api_path, json.dumps(body), _opts()]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    if r.returncode != 0:
        raise RuntimeError(f"IMA 调用失败 {api_path}: rc={r.returncode} {err[:400]}")
    try:
        resp = json.loads(out)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"IMA 返回非 JSON {api_path}: {out[:300]}") from e
    if resp.get("code") != 0:
        raise RuntimeError(f"IMA 业务错误 {api_path}: code={resp.get('code')} msg={resp.get('msg')}")
    return resp.get("data") or {}


def resolve_kb_id() -> str:
    """KB id 格式会变（10-23 就换过一次），每次现取，绝不硬编码。"""
    data = _api("openapi/wiki/v1/get_addable_knowledge_base_list", {"cursor": "", "limit": 50})
    for kb in data.get("addable_knowledge_base_list") or []:
        if kb.get("name") == KB_NAME:
            return kb["id"]
    raise RuntimeError(f"找不到知识库「{KB_NAME}」")


# ---------------------------------------------------------------- 读
def _parse_doc_title_version(title: str) -> tuple:
    """从文件名解析 (版本号, 时间戳) → 排序键。取不到返回 (0, 0)。"""
    m = re.search(r"_v(\d+)_(\d{8})_(\d{4})", title)
    if not m:
        return (0, 0)
    return (int(m.group(1)), int(m.group(2)) * 10000 + int(m.group(3)))


def list_analysis_docs(kb_id: str) -> list:
    """列出所有老师单分析文档（不含正文）。"""
    data = _api("openapi/wiki/v1/get_knowledge_list",
                {"knowledge_base_id": kb_id, "cursor": "", "limit": 50})
    # ⚠️ knowledge_list 是 dict 不是 list（10-23 踩过）
    kl = data.get("knowledge_list")
    if isinstance(kl, dict):
        items = kl.get("list") or kl.get("items") or []
    else:
        items = kl or []
    out = []
    for it in items:
        title = it.get("title") or ""
        if title.startswith(DOC_PREFIX):
            out.append({"title": title, "media_id": it.get("media_id"), "folder_id": it.get("folder_id")})
    return out


def fetch_doc_text(media_id: str) -> str:
    """取文档正文（markdown 原文）。"""
    data = _api("openapi/wiki/v1/get_media_info", {"media_id": media_id})
    url = ((data.get("url_info") or {}).get("url")) or ""
    if not url:
        raise RuntimeError(f"get_media_info 没返回 url: {media_id}")
    with urllib.request.urlopen(url, timeout=60) as resp:
        return resp.read().decode("utf-8", errors="replace")


def extract_json_block(text: str) -> dict:
    """从 markdown 正文里抠出 ```json code block``` 还原 dict。"""
    m = re.search(r"```json\s*\n(.*?)\n```", text, re.S)
    if not m:
        raise RuntimeError("文档里没找到 ```json``` code block")
    return json.loads(m.group(1))


def read_latest_analysis(kb_id: Optional[str] = None) -> Optional[dict]:
    """读最新版老师单分析。没有则返回 None。"""
    kb_id = kb_id or resolve_kb_id()
    docs = list_analysis_docs(kb_id)
    if not docs:
        return None
    docs.sort(key=lambda d: _parse_doc_title_version(d["title"]), reverse=True)
    text = fetch_doc_text(docs[0]["media_id"])
    data = extract_json_block(text)
    data["_ima_doc_title"] = docs[0]["title"]
    return data


# ---------------------------------------------------------------- 写
def build_doc_markdown(payload: dict, version: int) -> tuple:
    """生成 (文件名, markdown 正文)。正文 = 元信息 + ```json``` 全量。"""
    now = datetime.now(CST)
    fname = f"{DOC_PREFIX}{version}_{now.strftime('%Y%m%d_%H%M')}.md"
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    md = (
        f"# 老师单分析 v{version}\n\n"
        f"- **生成时间**: {now.strftime('%Y-%m-%d %H:%M:%S')} CST\n"
        f"- **数据源**: teacher_trades.jsonl + BTC_USDT_klines.json（本地只读）\n"
        f"- **用途**: 网页「数据分析」区老师单卡片的数据底座（IMA 当数据库）\n"
        f"- **笔数**: {payload.get('summary', {}).get('total')}\n\n"
        f"---\n\n"
        f"## 完整数据（JSON）\n\n"
        f"```json\n{body}\n```\n"
    )
    return fname, md


def upload_markdown(file_name: str, md_content: str, kb_id: str,
                    folder_id: Optional[str] = None) -> str:
    """走标准 markdown 上传流程。

    复用官方两个脚本（已跑通，别自己造 COS 签名）：
      preflight-check.cjs → 拿 file_size / content_type / media_type
      cos-upload.cjs      → 用临时凭证上传到 COS
    自己只编排 check_repeated_names → create_media → cos-upload → add_knowledge。

    ⚠️ KB 推土机：add_knowledge 之后不可撤销。写入前必过 preflight + 重名检测。
    """
    tmp = "/tmp/_teacher_analysis_upload.md"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(md_content)

    # 1) preflight（校验 UTF-8 / 大小 / 类型，给出 media_type）
    pf = subprocess.run(
        ["node", f"{SKILL_DIR}/knowledge-base/scripts/preflight-check.cjs", "--file", tmp],
        capture_output=True, text=True)
    if pf.returncode != 0:
        raise RuntimeError(f"preflight 失败: {pf.stderr[:400]}")
    meta = json.loads(pf.stdout)
    if not meta.get("pass"):
        raise RuntimeError(f"preflight pass=false: {meta}")
    file_size = int(meta["file_size"])
    content_type = meta["content_type"]
    media_type = int(meta["media_type"])

    # 2) 重名检测 —— 同名就换时间戳后缀（幂等，不覆盖旧文档）
    chk_body = {"params": [{"name": file_name, "media_type": media_type}],
                "knowledge_base_id": kb_id}
    if folder_id:
        chk_body["folder_id"] = folder_id
    chk = _api("openapi/wiki/v1/check_repeated_names", chk_body)
    results = chk.get("results") or [{}]
    if results[0].get("is_repeated"):
        base, ext = os.path.splitext(file_name)
        file_name = f"{base}_{datetime.now(CST).strftime('%Y%m%d%H%M%S')}{ext}"

    # 3) create_media → 临时 COS 凭证
    cr = {"file_name": file_name, "file_size": file_size, "content_type": content_type,
          "knowledge_base_id": kb_id, "file_ext": "md", "media_type": media_type}
    if folder_id:
        cr["folder_id"] = folder_id
    cm = _api("openapi/wiki/v1/create_media", cr)
    media_id = cm.get("media_id")
    if not media_id:
        raise RuntimeError(f"create_media 没返回 media_id: {cm}")
    cred = cm.get("cos_credential") or {}
    for k in ("secret_id", "secret_key", "token", "bucket_name", "region", "cos_key"):
        if not cred.get(k):
            raise RuntimeError(f"cos_credential 缺字段 {k}: {cred}")

    # 4) COS 上传（官方脚本）
    up = subprocess.run(
        ["node", f"{SKILL_DIR}/knowledge-base/scripts/cos-upload.cjs",
         "--file", tmp,
         "--secret-id", cred["secret_id"], "--secret-key", cred["secret_key"],
         "--token", cred["token"], "--bucket", cred["bucket_name"],
         "--region", cred["region"], "--cos-key", cred["cos_key"],
         "--content-type", content_type,
         "--start-time", str(cred["start_time"]), "--expired-time", str(cred["expired_time"]),
         "--timeout", "600000"],
        capture_output=True, text=True)
    if up.returncode != 0:
        raise RuntimeError(f"cos 上传失败: {(up.stderr or up.stdout)[:400]}")

    # 5) add_knowledge（不可逆 —— 到这一步就成了）
    add = {"media_type": media_type, "media_id": media_id, "title": file_name,
           "knowledge_base_id": kb_id,
           "file_info": {"cos_key": cred["cos_key"], "file_size": file_size,
                         "file_name": file_name}}
    if folder_id:
        add["folder_id"] = folder_id
    _api("openapi/wiki/v1/add_knowledge", add)
    return media_id


def next_version(kb_id: str) -> int:
    docs = list_analysis_docs(kb_id)
    if not docs:
        return 1
    return max(_parse_doc_title_version(d["title"])[0] for d in docs) + 1


def save_analysis(payload: dict, kb_id: Optional[str] = None,
                   folder_id: Optional[str] = None) -> dict:
    """把分析结果写进 IMA KB。返回 {version, title, media_id}。"""
    kb_id = kb_id or resolve_kb_id()
    version = next_version(kb_id)
    fname, md = build_doc_markdown(payload, version)
    media_id = upload_markdown(fname, md, kb_id, folder_id)
    return {"version": version, "title": fname, "media_id": media_id, "kb_id": kb_id}


# ---------------------------------------------------------------- CLI
if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "status":
        kb = resolve_kb_id()
        docs = list_analysis_docs(kb)
        docs.sort(key=lambda d: _parse_doc_title_version(d["title"]), reverse=True)
        print(f"KB: {KB_NAME} ({kb})")
        print(f"分析文档 {len(docs)} 份：")
        for d in docs[:10]:
            print("  -", d["title"])
        if docs:
            data = read_latest_analysis(kb)
            s = (data or {}).get("summary") or {}
            print("最新版本战绩:", json.dumps(s, ensure_ascii=False))
    elif cmd == "read":
        print(json.dumps(read_latest_analysis(), ensure_ascii=False, indent=2))
    elif cmd == "write":
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from teacher_analysis import build_teacher_analysis
        payload = build_teacher_analysis()
        res = save_analysis(payload)
        print(json.dumps(res, ensure_ascii=False))
    else:
        print("usage: teacher_kb_store.py [status|read|write]")
