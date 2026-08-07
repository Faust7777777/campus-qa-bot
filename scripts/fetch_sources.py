# -*- coding: utf-8 -*-
"""
C 类主题找源抓取器
对 official_search_and_verify 的 8 个主题, 抓取已定位的官方源正文
输出: work/fetch_output_search_sources.jsonl (Luna 协议格式)
"""

import json
import time
import hashlib
import datetime
import re
import os
import requests

BASE = r"<REPO>"
OUT = os.path.join(BASE, "work", "fetch_output_search_sources.jsonl")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
}

# 主题 -> 官方源列表 (找源结果)
SOURCES = [
    # 组织关系转接
    (
        "组织关系转接-团员",
        "https://me.dlut.edu.cn/info/1065/14356.htm",
        "大连理工大学机械工程学院2025级研究生团员智慧团建线上团组织关系转接操作流程",
    ),
    (
        "组织关系转接-团员",
        "https://ee.dlut.edu.cn/info/1313/23014.htm",
        "关于开展硕博新生团关系转接工作的通知",
    ),
    (
        "组织关系转接-团员",
        "https://ee.dlut.edu.cn/info/1313/13343.htm",
        "关于开展毕业生团关系转接工作的通知",
    ),
    (
        "组织关系转接-党员",
        "https://chem.dlut.edu.cn/info/1041/2378.htm",
        "大连理工大学关于进一步加强党员组织关系管理的规定（暂行）",
    ),
    (
        "组织关系转接-党员",
        "https://fhss.dlut.edu.cn/info/1281/37312.htm",
        "关于2026年新生组织关系转接工作的通知",
    ),
    (
        "组织关系转接-党员",
        "https://mse.dlut.edu.cn/info/1071/4841.htm",
        "材料学院组织关系转接工作规范",
    ),
    (
        "组织关系转接-党员",
        "https://ee.dlut.edu.cn/info/1313/13413.htm",
        "电气工程学院党委关于2025级研究生新生党员组织关系转接的说明",
    ),
    # 户口迁移
    ("户口迁移", "https://gach.dlut.edu.cn/info/1075/11654.htm", "新生办理落户指南"),
    ("户口迁移", "https://gach.dlut.edu.cn/bszn/hjfw.htm", "户籍服务-保卫处"),
    (
        "户口迁移",
        "https://yx.dlut.edu.cn/info/9990/89866.htm",
        "大连理工大学2025年普通类新生报到须知",
    ),
    # 心理咨询
    (
        "心理咨询",
        "https://xinli.dlut.edu.cn/zx/zxyy.htm",
        "咨询预约-心理健康教育与咨询中心",
    ),
    (
        "心理咨询",
        "https://xinli.dlut.edu.cn/zxgk/lxwm.htm",
        "联系我们-心理健康教育与咨询中心",
    ),
    # 社团注册
    (
        "社团注册",
        "https://chuangxin.dlut.edu.cn/info/1043/3819.htm",
        "大连理工大学学生社团协会管理办法",
    ),
    ("社团注册", "https://yx.dlut.edu.cn/xszz/xsst.htm", "学生社团-迎新网"),
    # 志愿者
    (
        "志愿者",
        "https://pjxqtw.dlut.edu.cn/info/1057/11455.htm",
        "盘锦校区志愿者注册工作通知",
    ),
    (
        "志愿者",
        "https://dli.dlut.edu.cn/info/1531/11631.htm",
        "莱斯特国际学院志愿辽宁注册通知",
    ),
    # 讲座论坛
    ("讲座论坛", "https://huodong.dlut.edu.cn/hdxz.htm", "活动须知-校内活动网"),
    (
        "讲座论坛",
        "https://chuangxin.dlut.edu.cn/info/1216/8788.htm",
        "报告会论坛讲座申请审核备案系统使用指南",
    ),
    (
        "讲座论坛",
        "https://aaschool.dlut.edu.cn/info/1029/2426.htm",
        "转发大连理工大学报告会论坛和讲座管理办法",
    ),
    # 实验室
    (
        "实验室预约",
        "https://iac.dlut.edu.cn/info/1058/1098.htm",
        "大连理工大学大型仪器设备开放共享系统管理细则",
    ),
    (
        "实验室预约",
        "https://iac.dlut.edu.cn/info/1057/1272.htm",
        "分析测试中心开放管理办法",
    ),
    (
        "实验室预约",
        "https://biolab.dlut.edu.cn/info/1041/1038.htm",
        "学生须知-生物实验教学中心",
    ),
]


def extract_body(html):
    frag = re.sub(
        r"<script[\s\S]*?</script>|<style[\s\S]*?</style>|<!--[\s\S]*?-->", " ", html
    )
    frag = re.sub(r"<(br|/p|/div|/li|/h[1-6]|/tr)[^>]*>", "\n", frag, flags=re.I)
    frag = re.sub(r"<[^>]+>", " ", frag)
    frag = re.sub(r"[ \t\xa0]+", " ", frag)
    frag = re.sub(r"\n\s*\n+", "\n", frag)
    return frag.strip()


def main():
    done = set()
    if os.path.exists(OUT):
        try:
            with open(OUT, encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        try:
                            done.add(json.loads(line)["source_id"])
                        except (json.JSONDecodeError, KeyError):
                            pass
        except OSError:
            pass

    ok = 0
    fail = 0
    try:
        out = open(OUT, "a", encoding="utf-8")
    except OSError as ex:
        print(f"打开输出失败: {ex}")
        return
    with out:
        for topic, url, title in SOURCES:
            sid = f"search:{topic}:{hashlib.sha256(url.encode()).hexdigest()[:16]}"
            if sid in done:
                continue
            try:
                r = requests.get(url, headers=HEADERS, timeout=20, allow_redirects=True)
                r.encoding = r.apparent_encoding or "utf-8"
                text = extract_body(r.text) if r.status_code == 200 else ""
                if r.status_code == 200 and len(text) >= 30:
                    status = "success"
                    ok += 1
                else:
                    status = "fetch_failed"
                    text = ""
                    fail += 1
            except (requests.RequestException, ValueError, TypeError):
                status = "fetch_failed"
                text = ""
                fail += 1
            rec = {
                "source_id": sid,
                "dataset": "web_plus_index",
                "canonical_url": url,
                "title": title,
                "official_domain": url.split("/")[2],
                "published_at": None,
                "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest()
                if text
                else "",
                "clean_text": text,
                "fetch_status": status,
                "topic": topic,
                "candidate_cards": [],
                "unresolved_questions": [],
            }
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            out.flush()
            time.sleep(0.4)

    print(f"完成: success {ok} / failed {fail} → {OUT}")


if __name__ == "__main__":
    main()
