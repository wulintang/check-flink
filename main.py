import os
import csv
import json
import time
import logging
import requests
import warnings
from queue import Queue
from datetime import datetime
from urllib.parse import urlparse
import concurrent.futures

# 设置日志
logging.basicConfig(
    level=logging.INFO,
    format="😎 %(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)

warnings.filterwarnings("ignore", message="Unverified HTTPS request is being made.*")

# ========== 核心新增：白名单配置（按需修改） ==========
# 1. 访问白名单：这些链接跳过访问检测，直接标记为"可访问"
ACCESS_WHITELIST = [
    "https://www.gymxbl.com/",
    "https://www.quji.org/",
    "https://www.52txr.cn/",
    "https://www.dalao.net/"
    "https://www.zhaoxuze.com/",
    # 可继续添加需要跳过访问检测的链接
]

# 2. 反链白名单：这些链接跳过反链检测，直接标记为"有反链"
LINK_WHITELIST = [
    "https://www.gymxbl.com/",
    "https://www.quji.org/",
    "https://www.52txr.cn/",
    "https://www.hansjack.com/",
    "https://blog.ciraos.top/",
    "http://puo.cn/",
    "https://www.dalao.net/",
    "https://www.s17.cn/",
    "https://siitake.cn/",
    "https://miraii.cn/",
    "https://whitebear.im/",
    "https://www.liuzhixi.cn/",
    "https://www.liaao.cn/",
    "https://limitz.top/",
    "https://www.lihaoyu.cn/",
    "https://www.imuu.cn/",
    "https://www.liuzhixi.cn/",
    "https://u.sb/"
    # 可继续添加需要跳过反链检测的链接
]
# =====================================================

# 请求头统一配置
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36 "
        "(check-flink/2.0; +https://github.com/willow-god/check-flink)"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "X-Check-Flink": "1.0"
}

RAW_HEADERS = {  # 仅用于获取原始数据，防止接收到Accept-Language等头部导致乱码
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36 "
        "(check-flink/2.0; +https://github.com/willow-god/check-flink)"
    ),
    "X-Check-Flink": "2.0"
}

# 修复：添加SOURCE_URL兜底，避免None
PROXY_URL_TEMPLATE = f"{os.getenv('PROXY_URL')}{{}}" if os.getenv("PROXY_URL") else None
SOURCE_URL = os.getenv("SOURCE_URL", "./link.csv")  # 恢复兜底值
RESULT_FILE = "./result.json"
AUTHOR_URL = os.getenv("AUTHOR_URL", "www.dao.js.cn")  # 作者URL，用于检测反链
api_request_queue = Queue()

if PROXY_URL_TEMPLATE:
    logging.info("代理 URL 获取成功，代理协议: %s", PROXY_URL_TEMPLATE.split(":")[0])
else:
    logging.info("未提供代理 URL")

if AUTHOR_URL:
    logging.info("作者 URL: %s", AUTHOR_URL)
else:
    logging.warning("未提供作者 URL，将跳过友链页面检测")

def request_url(session, url, headers=HEADERS, desc="", timeout=15, verify=True, **kwargs):
    """统一封装的 GET 请求函数"""
    try:
        start_time = time.time()
        response = session.get(url, headers=headers, timeout=timeout, verify=verify, **kwargs)
        latency = round(time.time() - start_time, 2)
        return response, latency
    except requests.RequestException as e:
        logging.warning(f"[{desc}] 请求失败: {url}，错误如下: \n================================================================\n{e}\n================================================================")
        return None, -1

def load_previous_results():
    if os.path.exists(RESULT_FILE):
        try:
            with open(RESULT_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            logging.warning("JSON 解析错误，使用空数据")
    return {}

def save_results(data):
    with open(RESULT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

def is_url(path):
    return urlparse(path).scheme in ("http", "https")

def check_author_link_in_page(session, linkpage_url):
    """检测友链页面是否包含作者链接"""
    if not AUTHOR_URL:
        return False
    
    response, _ = request_url(session, linkpage_url, headers=RAW_HEADERS, desc="友链页面检测")
    if not response:
        return False
    
    # 处理作者URL，确保有协议号
    author_url = AUTHOR_URL
    if not author_url.startswith(('http://', 'https://')):
        author_url = 'https://' + author_url
    
    # 生成各种可能的URL变体
    author_variants = [
        author_url,
        author_url.replace('https://', 'http://'),
        author_url.replace('https://', '//'),
        author_url.replace('https://', ''),
        AUTHOR_URL,  # 原始值（可能没有协议号）
        '//' + AUTHOR_URL,
        'https://' + AUTHOR_URL,
        'http://' + AUTHOR_URL
    ]
    
    # 去重
    author_variants = list(set(author_variants))
    
    content = response.text
    found_in_href = False
    found_as_text = False
    
    # 检查每种变体
    for variant in author_variants:
        # 检查是否在href属性中
        if f'href="{variant}"' in content or \
           f"href='{variant}'" in content or \
           f'href="{variant}/"' in content or \
           f"href='{variant}/'" in content:
            found_in_href = True
            break
        
        # 检查是否作为文本出现
        if variant in content:
            found_as_text = True
    
    if found_in_href:
        logging.info(f"友链页面 {linkpage_url} 中找到作者链接: {author_url}")
        return True
    elif found_as_text:
        logging.info(f"友链页面 {linkpage_url} 中包含作者URL文本但非链接")
        return True
    else:
        logging.info(f"友链页面 {linkpage_url} 中未找到作者链接")
        return False

def fetch_origin_data(origin_path):
    logging.info(f"正在读取数据源: {origin_path}")
    try:
        if is_url(origin_path):
            with requests.Session() as session:
                response, _ = request_url(session, origin_path, headers=RAW_HEADERS, desc="数据源")
                content = response.text if response else ""
        else:
            with open(origin_path, "r", encoding="utf-8") as f:
                content = f.read()
    except Exception as e:
        logging.error(f"读取数据失败: {e}")
        return []

    try:
        data = json.loads(content)
        if isinstance(data, dict) and 'link_list' in data:
            logging.info("成功解析 JSON 格式数据")
            return data['link_list']
        elif isinstance(data, list):
            logging.info("成功解析 JSON 数组格式数据")
            return data
    except json.JSONDecodeError:
        pass

    try:
        rows = list(csv.reader(content.splitlines()))
        logging.info("成功解析 CSV 格式数据")
        # 支持新的CSV格式：name, link, linkpage
        result = []
        for row in rows:
            if len(row) >= 2:
                item = {'name': row[0], 'link': row[1]}
                if len(row) >= 3 and row[2].strip():
                    item['linkpage'] = row[2].strip()
                result.append(item)
        return result
    except Exception as e:
        logging.error(f"CSV 解析失败: {e}")
        return []

def check_link(item, session):
    link = item['link']
    has_author_link = False
    
    # ========== 新增：白名单判断逻辑 ==========
    # 1. 访问白名单：直接标记为可访问（latency设为0，代表跳过检测）
    if link in ACCESS_WHITELIST:
        logging.info(f"[白名单] {link} 属于访问白名单，跳过访问检测，标记为可访问")
        # 2. 反链白名单：直接标记为有反链
        has_author_link = True if link in LINK_WHITELIST else False
        if link in LINK_WHITELIST:
            logging.info(f"[白名单] {link} 属于反链白名单，跳过反链检测，标记为有反链")
        return item, 0, has_author_link  # 0代表跳过检测的可访问状态
    # =========================================
    
    for method, url in [("直接访问", link), ("代理访问", PROXY_URL_TEMPLATE.format(link) if PROXY_URL_TEMPLATE else None)]:
        if not url or not is_url(url):
            logging.warning(f"[{method}] 无效链接: {link}")
            continue
        response, latency = request_url(session, url, desc=method)
        if response and response.status_code == 200:
            logging.info(f"[{method}] 成功访问: {link} ，延迟 {latency} 秒")
            
            # 如果链接可达且有linkpage字段，检测友链页面（反链白名单已提前判断）
            if 'linkpage' in item and item['linkpage'] and AUTHOR_URL and link not in LINK_WHITELIST:
                has_author_link = check_author_link_in_page(session, item['linkpage'])
            
            return item, latency, has_author_link
        elif response and response.status_code != 200:
            logging.warning(f"[{method}] 状态码异常: {link} -> {response.status_code}")
        else:
            logging.warning(f"[{method}] 请求失败，Response 无效: {link}")

    api_request_queue.put(item)
    return item, -1, has_author_link

import time
import logging
# 假设以下变量和函数已在其他地方定义
# api_request_queue, ACCESS_WHITELIST, LINK_WHITELIST, RAW_HEADERS, AUTHOR_URL
# request_url(), check_author_link_in_page()

def handle_api_requests(session):
    results = []
    while not api_request_queue.empty():
        time.sleep(0.2)
        item = api_request_queue.get()
        link = item['link']
        
        # ========== 新增：API检测阶段也判断白名单 ==========
        if link in ACCESS_WHITELIST:
            logging.info(f"[白名单] {link} 属于访问白名单，跳过API检测，标记为可访问")
            has_author_link = True if link in LINK_WHITELIST else False
            if link in LINK_WHITELIST:
                logging.info(f"[白名单] {link} 属于反链白名单，跳过反链检测，标记为有反链")
            results.append((item, 0, has_author_link))
            continue
        # ================================================
        
        # 替换为最终确定的新API地址
        api_url = f"https://uapis.cn/api/v1/network/urlstatus?url={link}"
        response, latency = request_url(session, api_url, headers=RAW_HEADERS, desc="API 检查", timeout=30)
        has_author_link = False
        
        if response:
            try:
                res_json = response.json()
                # 适配新接口：仅判断status字段是否为200（HTTP成功状态）
                if int(res_json.get("status")) == 200:
                    logging.info(f"[API] 成功访问: {link} ，HTTP状态码 {res_json.get('status')}")
                    item['latency'] = latency
                    
                    # 反链白名单判断逻辑保持不变
                    if link in LINK_WHITELIST:
                        has_author_link = True
                        logging.info(f"[白名单] {link} 属于反链白名单，标记为有反链")
                    elif 'linkpage' in item and item['linkpage'] and AUTHOR_URL:
                        has_author_link = check_author_link_in_page(session, item['linkpage'])
                else:
                    # 输出目标URL的HTTP异常状态码
                    logging.warning(
                        f"[API] 状态异常: {link} -> [目标HTTP状态码: {res_json.get('status')}]"
                    )
                    item['latency'] = -1
            except Exception as e:
                logging.error(f"[API] 解析响应失败: {link}，错误: {e}")
                item['latency'] = -1
        else:
            # API请求本身失败（无响应）
            logging.warning(f"[API] 请求失败: {link} ，未获取到响应")
            item['latency'] = -1
        
        results.append((item, item.get('latency', -1), has_author_link))
    return results

def main():
    try:
        # 打印白名单配置，方便验证
        logging.info(f"=== 白名单配置 ===")
        logging.info(f"访问白名单: {ACCESS_WHITELIST}")
        logging.info(f"反链白名单: {LINK_WHITELIST}")
        logging.info(f"==================")
        
        link_list = fetch_origin_data(SOURCE_URL)
        if not link_list:
            logging.error("数据源为空或解析失败")
            return

        previous_results = load_previous_results()

        with requests.Session() as session:
            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                results = list(executor.map(lambda item: check_link(item, session), link_list))

            updated_api_results = handle_api_requests(session)
            for updated_item in updated_api_results:
                for idx, (item, latency, has_author) in enumerate(results):
                    if item['link'] == updated_item[0]['link']:
                        results[idx] = updated_item
                        break

        current_links = {item['link'] for item in link_list}
        link_status = []

        for item, latency, has_author_link in results:
            try:
                name = item.get('name', '未知')
                link = item.get('link')
                if not link:
                    logging.warning(f"跳过无效项: {item}")
                    continue

                prev_entry = next((x for x in previous_results.get("link_status", []) if x.get("link") == link), {})
                prev_fail_count = prev_entry.get("fail_count", 0)
                # 白名单链接（latency=0）不计入失败次数
                fail_count = prev_fail_count + 1 if latency == -1 else 0

                link_status.append({
                    'name': name,
                    'link': link,
                    'latency': latency,
                    'fail_count': fail_count,
                    'has_author_link': has_author_link,
                    'linkpage': item.get('linkpage', '')
                })
            except Exception as e:
                logging.error(f"处理链接时发生错误: {item}, 错误: {e}")

        link_status = [entry for entry in link_status if entry["link"] in current_links]

        accessible = sum(1 for x in link_status if x["latency"] != -1)
        has_author_count = sum(1 for x in link_status if x["has_author_link"])
        total = len(link_status)
        output = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "accessible_count": accessible,
            "inaccessible_count": total - accessible,
            "total_count": total,
            "has_author_link_count": has_author_count,
            "author_url": AUTHOR_URL,
            "link_status": link_status
        }

        save_results(output)
        logging.info(f"共检查 {total} 个链接，成功 {accessible} 个，失败 {total - accessible} 个")
        logging.info(f"其中 {has_author_count} 个友链页面包含作者链接")
        logging.info(f"结果已保存至: {RESULT_FILE}")
    except Exception as e:
        logging.exception(f"运行主程序失败: {e}")

if __name__ == "__main__":
    main()
