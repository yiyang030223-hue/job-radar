#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
国聘网（iguopin.com）岗位采集脚本 —— 经济岗位雷达 第1个真实数据源
按「采集提示词-国聘网.md」实现：
  - 三类 nature（实习/校招/社招），POST JSON 接口
  - 翻页去重（job_id），超出边界返回重复页 -> 自动停止
  - 限速 / 重试 / 异常记录隔离
  - 字段映射到原型 schema，本地分类（经济类/教育类）
输出：jobs.json（真实数据，schema 对齐原型）+ 采集日志
"""
import json
import os
import random
import re
import sys
import time
try:
    import requests
except ModuleNotFoundError:
    print("缺少 requests 库，请先运行：python -m pip install -r requirements.txt", file=sys.stderr)
    raise SystemExit(3)

BASE = os.path.dirname(os.path.abspath(__file__))
API = "https://gp-api.iguopin.com/api/jobs/v1/list"
PAGE_SIZE = 200
MAX_PAGES = int(sys.argv[1]) if len(sys.argv) > 1 else 8   # 每类最多翻几页（原始 200/页）
SLEEP = 1.6
OUT_JSON = os.path.join(BASE, "jobs.json")

HEADERS = {
    "Content-Type": "application/json;charset=UTF-8",
    "Accept": "application/json, text/plain, */*",
    "Device": "pc", "Version": "5.0.0",
    "Origin": "https://www.iguopin.com", "Referer": "https://www.iguopin.com/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
}

# 招聘类型
NATURE = {"intern": "11bTac9", "campus": "115xW5oQ", "social": "113Fc6wc"}
NATURE_CN = {"intern": "实习", "campus": "校招", "social": "社招"}

# 地区：只采集北京（area_code 层级 国家.省；北京=000000.110000）
DISTRICT = ["000000.110000"]

# ---- 分类关键词 ----
# 岗位文本（岗位名/类别/专业/行业）里出现即视为经济类，较可靠
ECON_JOB_KW = ["金融", "经济", "财务", "会计", "银行", "证券", "基金", "保险", "审计",
               "风控", "税务", "财政", "信贷", "融资", "期货", "信托", "理财", "资管",
               "出纳", "预算", "合规", "反洗钱", "货币", "投行", "财会", "统计",
               "投资", "资产管理", "资本运作", "担保", "资产评估", "财务"]
# 公司名里明确的金融机构（"投资有限公司"这种泛称不算，避免误判实业公司）
ECON_CO_KW = ["银行", "证券", "保险", "会计师事务所", "基金", "信托", "信用社",
              "交易所", "金融", "期货", "资产管理", "资本管理", "投资管理", "基金管理",
              "财务公司", "金控", "融资租赁"]
# 教学岗位强信号
EDU_JOB_KW = ["教师", "老师", "讲师", "教研", "教学", "辅导员", "教务", "教授",
              "课程", "教材", "幼教", "学前", "师范", "教育", "导师", "培训师", "培训讲师"]
EDU_CO_KW = ["学校", "学院", "大学", "幼儿园", "中学", "小学", "国际学校",
             "师范", "高校", "教育科技", "教育培训"]

# 经济类细分 key（与原型 IND_MAP 对齐）
def econ_subkey(text, company):
    if re.search(r"人民银行|开发银行|进出口银行|农业发展银行", company): return "policy"
    if re.search(r"证券|证券公司|券商", text): return "securities"
    if re.search(r"基金", text): return "fund"
    if re.search(r"保险", text): return "insurance"
    if re.search(r"会计师事务所|普华永道|德勤|安永|毕马威", text): return "accounting"
    if re.search(r"审计", text): return "accounting"
    if re.search(r"咨询", text): return "consulting"
    if re.search(r"监管|监督管理|证监会|银保监", text): return "regulator"
    if re.search(r"金融科技|金科", text): return "fintech"
    if re.search(r"国际贸易|外贸|进出口", text): return "trade"
    if re.search(r"银行", company): return "bank"
    if re.search(r"投资|资本|资管|融资|资产", text): return "corporate"
    return "corporate"

# 教育类细分 key
def edu_subkey(text, company):
    if re.search(r"教育局|教委|教育行政|教科院", text): return "edu_gov"
    if re.search(r"教研", text): return "edu_gov"
    if re.search(r"辅导员", text): return "uni_staff"
    if re.search(r"国际学校", company): return "intl"
    if re.search(r"中学|小学|幼儿园|学前|幼教", text): return "school"
    if re.search(r"出版|教材|传媒", text + " " + company): return "pub"
    if re.search(r"培训师|培训讲师|留学|培训", text): return "train"
    if re.search(r"在线教育|教育科技|网课|网络教育", text): return "edtech"
    if re.search(r"大学|学院|高校|教授|科研|教学", text): return "uni_teach"
    return "edu_gov"

def classify(job_text, company):
    """返回 (大类, 细分key)。
    规则：教学强信号→教育；医疗岗排除；岗位文本经济强信号→经济；
    再由明确持牌金融机构 / 明确教育机构兜底；模糊名（教育科技/投资集团）与工科岗不收。"""
    teach = ["教师", "老师", "讲师", "教研", "教学", "辅导员", "教务", "教授",
             "课程", "幼教", "学前", "师范", "导师", "培训师", "培训讲师"]
    if any(k in job_text for k in teach):
        return "edu", edu_subkey(job_text, company)
    if any(k in job_text for k in
           ["医师", "医生", "护士", "护理", "临床", "药师", "医技", "医务", "医院"]):
        return None, None
    if any(k in job_text for k in ECON_JOB_KW):
        return "econ", econ_subkey(job_text, company)
    strong_fin = ["银行", "证券", "保险", "会计师事务所", "基金", "信托",
                  "信用社", "交易所", "期货", "财务公司", "融资租赁"]
    if any(k in company for k in strong_fin):
        return "econ", econ_subkey(job_text, company)
    strong_edu = ["学校", "学院", "大学", "幼儿园", "中学", "小学",
                  "国际学校", "师范", "高校"]
    if any(k in company for k in strong_edu):
        return "edu", edu_subkey(job_text, company)
    return None, None

def map_ownership(nature_cn):
    if not nature_cn: return "priv"
    if re.search(r"国企|央企|国有", nature_cn): return "soe"
    if re.search(r"事业单位|机关", nature_cn): return "gov"
    if re.search(r"外资|外商|中外|合资|港|台", nature_cn): return "fgn"
    return "priv"  # 民营/私营/股份制

def parse_city(area_cn):
    if not isinstance(area_cn, str) or not area_cn.strip(): return "全国"
    s = area_cn.split("-")[0].strip()
    return s or "全国"

def clean_text(value, default=""):
    """把接口中的标量安全转换为去除首尾空白的文本。"""
    if value is None:
        return default
    if isinstance(value, (str, int, float)):
        text = str(value).strip()
        return text or default
    return default

def clean_list(value):
    """只保留列表中可用的文本项，避免异常字段破坏整批采集。"""
    if not isinstance(value, list):
        return []
    return [text for item in value if (text := clean_text(item))]

def fmt_salary(j):
    if j.get("is_negotiable"): return "面议"
    lo, hi, unit = j.get("min_wage"), j.get("max_wage"), j.get("wage_unit_cn", "")
    if not lo and not hi: return ""
    try:
        lo, hi = float(lo or hi), float(hi or lo)
    except (TypeError, ValueError):
        return ""
    if "月" in unit:
        k1, k2 = round(lo/1000), round(hi/1000)
        if k1 == k2: return f"{k1}K"
        return f"{k1}-{k2}K"
    if "天" in unit:
        return f"{lo:g}-{hi:g}元/天"
    if "年" in unit:
        return f"{round(lo/10000)}-{round(hi/10000)}万/年"
    return f"{lo:g}-{hi:g}"

def split_items(seg):
    """把一段文本按编号行拆成条目，去掉编号前缀与噪声。"""
    seg = seg.replace("\r", "\n")
    lines = [l.strip(" ；;：:") for l in seg.split("\n") if l.strip(" ；;：:")]
    noise = {"描述", "说明", "备注", "职责", "要求", "】", "【", "无", "略", "（）"}
    out = []
    for l in lines:
        l = re.sub(r"^[0-9]{1,2}\s*[\.、\)）]\s*", "", l).strip(" ；;：:")
        if not l or l in noise: continue
        if re.fullmatch(r"[\W_]+", l): continue   # 纯标点
        if len(l.strip()) < 2: continue           # 单字噪声（换行拆散的字）
        out.append(l)
    if not out and seg.strip():
        out = [s.strip(" ；;：:") for s in re.split(r"[；;]", seg)
               if s.strip(" ；;：:") and s.strip() not in noise]
    return out

def parse_contents(c):
    """拆出职责、要求。"""
    if not c: return [], []
    # 统一标题
    c = c.replace("任职资格", "岗位要求").replace("任职要求", "岗位要求")
    resp, req = [], []
    m_resp = re.search(r"(岗位职责|工作职责|工作内容|职责)[：:]?\s*", c)
    m_req = re.search(r"(岗位要求|岗位任职要求|任职条件|招聘条件)[：:]?\s*", c)
    if m_resp:
        start = m_resp.end()
        end = m_req.start() if m_req and m_req.start() > start else len(c)
        resp = split_items(c[start:end])
    if m_req:
        start = m_req.end()
        # 要求到“福利”等结束
        m_stop = re.search(r"(职工福利|福利待遇|薪资福利)", c[start:])
        end = start + m_stop.start() if m_stop else len(c)
        req = split_items(c[start:end])
    if not resp and not req:
        resp = split_items(c)
    return resp, req

def map_job(j, ty):
    if not isinstance(j, dict): return None
    job_id = clean_text(j.get("job_id"))
    title = clean_text(j.get("job_name"))
    company = clean_text(j.get("company_name"))
    if not job_id or not title or not company: return None
    major = clean_list(j.get("major_cn"))
    inds = clean_list(j.get("industry_cn"))
    cat = clean_text(j.get("category_cn"))
    job_text = " ".join([title, cat, " ".join(major), " ".join(inds)])
    big, sub = classify(job_text, company)
    if not big: return None
    ci = j.get("company_info") or {}
    resp, req = parse_contents(clean_text(j.get("contents")))
    districts = j.get("district_list")
    first_district = districts[0] if isinstance(districts, list) and districts and isinstance(districts[0], dict) else {}
    area = first_district.get("area_cn", "")
    city = parse_city(area)
    if city != "北京": return None   # 本地兜底：只保留北京
    tags = list(dict.fromkeys((j.get("job_custom_tags_cn") or []) + major[:2] + inds[:1]))[:4]
    about = ci.get("name", company)
    if ci.get("industry_cn") or ci.get("scale_cn"):
        about = f"{company}（{ci.get('industry_cn','')}，{ci.get('scale_cn','')}）"
    return {
        "id": job_id,
        "t": title,
        "c": company,
        "o": map_ownership(ci.get("nature_cn")),
        "i": sub,
        "cat": big,
        "ty": ty,
        "city": city,
        "edu": clean_text(j.get("education_cn"), "不限"),
        "pub": clean_text(j.get("refresh_time") or j.get("start_time"))[:10],
        "dl": clean_text(j.get("end_time"))[:10],
        "sal": fmt_salary(j),
        "hot": 0,
        "src": "国聘网",
        "tags": tags,
        "resp": resp[:6] if resp else ["详见职位描述"],
        "req": req[:6] if req else ["详见职位要求"],
        "about": about,
        "url": "https://www.iguopin.com/job/detail?id=" + job_id,
    }

def call_api(ty, page, retries=3):
    body = {"page": page, "page_size": PAGE_SIZE, "keyword": "",
            "nature": [NATURE[ty]], "district": DISTRICT}
    for attempt in range(retries):
        try:
            r = requests.post(API, headers=HEADERS, json=body, timeout=25)
            r.raise_for_status()
            payload = r.json()
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, dict):
                raise ValueError("接口返回的数据结构无效")
            return data
        except Exception as e:
            if attempt == retries - 1: raise
            time.sleep(1.5 ** (attempt + 1))
    return None

def write_json_atomic(path, payload):
    """先写临时文件并完成替换，采集失败时保留上一份 jobs.json。"""
    temp_path = path + ".tmp"
    with open(temp_path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temp_path, path)

def main():
    seen = set()
    jobs = []
    log = []
    for ty in ["intern", "campus", "social"]:
        new_cnt = 0
        for page in range(1, MAX_PAGES + 1):
            try:
                d = call_api(ty, page)
            except Exception as e:
                log.append(f"{NATURE_CN[ty]} page{page} 失败: {e}")
                break
            if not d:
                log.append(f"{NATURE_CN[ty]} page{page} 返回为空，跳过")
                time.sleep(SLEEP); continue
            lst = d.get("list") or []
            page_ids = [clean_text(x.get("job_id")) for x in lst if isinstance(x, dict)]
            if not lst:
                log.append(f"{NATURE_CN[ty]} 已无更多数据（page{page} 空），停止")
                break
            if page > 1 and set(page_ids) & seen == set(page_ids) and page_ids:
                log.append(f"{NATURE_CN[ty]} 到达重复边界，停止于 page{page-1}")
                break
            added = 0
            for x in lst:
                jid = clean_text(x.get("job_id")) if isinstance(x, dict) else ""
                if not jid or jid in seen: continue
                seen.add(jid)
                try:
                    m = map_job(x, ty)
                except Exception as exc:
                    log.append(f"{NATURE_CN[ty]} page{page} 跳过异常岗位 {jid}: {exc}")
                    continue
                if m:
                    jobs.append(m); added += 1; new_cnt += 1
            log.append(f"{NATURE_CN[ty]} page{page}: 原始{len(lst)} 新增收录{added}")
            time.sleep(SLEEP + random.random() * 0.6)
        log.append(f"== {NATURE_CN[ty]} 本类型共收录 {new_cnt} ==")
    print("\n".join(log))
    if not jobs:
        print("采集失败：没有获得可用的北京岗位，已保留原 jobs.json。", file=sys.stderr)
        return 2
    write_json_atomic(OUT_JSON, jobs)
    # 分布统计
    from collections import Counter
    print("总收录:", len(jobs))
    print("大类:", dict(Counter(j["cat"] for j in jobs)))
    print("类型:", dict(Counter(j["ty"] for j in jobs)))
    print("性质:", dict(Counter(j["o"] for j in jobs)))
    print("城市Top:", Counter(j["city"] for j in jobs).most_common(8))
    print(f"更新成功：共 {len(jobs)} 个岗位")
    print(f"保存位置：{OUT_JSON}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
#（注：内容由AI生成）
