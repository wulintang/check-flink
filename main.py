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

# 请求头统一配置
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36 "
        "(check-flink/1.0; +https://github.com/willow-god/check-flink)"
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
        "(check-flink/1.0; +https://github.com/willow-god/check-flink)"
    ),
    "X-Check-Flink": "1.0"
}

# 转发服务基础URL
FORWARD_SERVICE_BASE = "https://github.snsou.cn/"
PROXY_URL_TEMPLATE = f"{os.getenv('PROXY_URL')}{{}}" if os.getenv("PROXY_URL") else None
SOURCE_URL = os.getenv("SOURCE_URL", "./link.csv")  # 默认本地文件
RESULT_FILE = "./result.json"
api_request_queue = Queue()

if PROXY_URL_TEMPLATE:
    logging.info("代理 URL 获取成功，代理协议: %s", PROXY_URL_TEMPLATE.split(":")[0])
else:
    logging.info("未提供代理 URL")

# 明确日志说明转发服务的拼接格式
logging.info(f"转发服务格式确认: 目标域名https://www.dao.js.cn 将访问 {FORWARD_SERVICE_BASE}https://www.dao.js.cn")

def request_url(session, url, headers=HEADERS, desc="", timeout=15, verify=True, **kwargs):
    """统一封装的 GET 请求函数"""
    try:
        start_time = time.time()
        logging.info(f"[{desc}] 开始请求: {url}")
        response = session.get(url, headers=headers, timeout=timeout, verify=verify,** kwargs)
        latency = round(time.time() - start_time, 2)
        logging.info(f"[{desc}] 请求完成: {url}，状态码: {response.status_code}，延迟: {latency} 秒")
        return response, latency
    except requests.RequestException as e:
        logging.warning(f"[{desc}] 请求失败: {url}，错误如下: \n{e}")
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
    logging.info(f"结果已保存至: {RESULT_FILE}")

def is_url(path):
    return urlparse(path).scheme in ("http", "https")

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
        return [{'name': row[0], 'link': row[1]} for row in rows if len(row) == 2]
    except Exception as e:
        logging.error(f"CSV 解析失败: {e}")
        return []

def check_link(item, session):
    link = item['link']
    logging.info(f"开始检查链接: {link}")
    
    # 优先尝试转发服务访问，严格按照指定格式拼接
    forward_url = f"{FORWARD_SERVICE_BASE}{link}"  # 例如：https://github.snsou.cn/ + https://www.dao.js.cn
    logging.info(f"[转发服务] 准备访问拼接后的URL: {forward_url} (原始链接: {link})")
    forward_response, forward_latency = request_url(session, forward_url, desc="转发服务访问")
    if forward_response and forward_response.status_code == 200:
        logging.info(f"[转发服务] 成功访问: {link} ，状态码 200，延迟 {forward_latency} 秒")
        return item, forward_latency
    
    # 转发服务失败后尝试其他方式
    logging.warning(f"[转发服务] 访问失败，尝试直接访问: {link}")
    direct_response, direct_latency = request_url(session, link, desc="直接访问")
    if direct_response and direct_response.status_code == 200:
        logging.info(f"[直接访问] 成功访问: {link} ，状态码 200，延迟 {direct_latency} 秒")
        return item, direct_latency
    
    if PROXY_URL_TEMPLATE:
        logging.warning(f"[直接访问] 失败，尝试代理访问: {link}")
        proxy_url = PROXY_URL_TEMPLATE.format(link)
        proxy_response, proxy_latency = request_url(session, proxy_url, desc="代理访问")
        if proxy_response and proxy_response.status_code == 200:
            logging.info(f"[代理访问] 成功访问: {link} ，状态码 200，延迟 {proxy_latency} 秒")
            return item, proxy_latency
    
    # 所有方式失败，加入API检查队列
    logging.info(f"[所有方式] 均失败，将 {link} 加入API检查队列")
    api_request_queue.put(item)
    return item, -1

def handle_api_requests(session):
    results = []
    logging.info(f"开始处理API检查队列，队列大小: {api_request_queue.qsize()}")
    while not api_request_queue.empty():
        time.sleep(0.2)
        item = api_request_queue.get()
        link = item['link']
        api_url = f"https://v2.xxapi.cn/api/status?url={link}"
        response, latency = request_url(session, api_url, headers=RAW_HEADERS, desc="API 检查", timeout=30)
        if response:
            try:
                res_json = response.json()
                if int(res_json.get("code")) == 200 and int(res_json.get("data")) == 200:
                    logging.info(f"[API] 成功访问: {link} ，状态码 200")
                    results.append((item, latency))
                else:
                    logging.warning(f"[API] 状态异常: {link} -> [{res_json.get('code')}, {res_json.get('data')}]")
                    results.append((item, -1))
            except Exception as e:
                logging.error(f"[API] 解析响应失败: {link}，错误: {e}")
                results.append((item, -1))
        else:
            logging.warning(f"[API] 请求失败: {link}")
            results.append((item, -1))
        api_request_queue.task_done()
    logging.info("API检查队列处理完成")
    return results

def main():
    try:
        link_list = fetch_origin_data(SOURCE_URL)
        if not link_list:
            logging.error("数据源为空或解析失败")
            return

        previous_results = load_previous_results()
        logging.info(f"加载历史结果: {len(previous_results.get('link_status', []))} 条记录")

        with requests.Session() as session:
            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                results = list(executor.map(lambda item: check_link(item, session), link_list))

            api_results = handle_api_requests(session)
            api_link_map = {item['link']: latency for item, latency in api_results}
            for i in range(len(results)):
                item, latency = results[i]
                if latency == -1 and item['link'] in api_link_map:
                    results[i] = (item, api_link_map[item['link']])

        current_links = {item['link'] for item in link_list}
        link_status = []

        for item, latency in results:
            try:
                name = item.get('name', '未知')
                link = item.get('link')
                if not link:
                    logging.warning(f"跳过无效项: {item}")
                    continue

                prev_entry = next((x for x in previous_results.get("link_status", []) if x.get("link") == link), {})
                prev_fail_count = prev_entry.get("fail_count", 0)
                fail_count = prev_fail_count + 1 if latency == -1 else 0

                link_status.append({
                    'name': name,
                    'link': link,
                    'latency': latency,
                    'fail_count': fail_count
                })
            except Exception as e:
                logging.error(f"处理链接时发生错误: {item}, 错误: {e}")

        link_status = [entry for entry in link_status if entry["link"] in current_links]

        accessible = sum(1 for x in link_status if x["latency"] != -1)
        total = len(link_status)
        output = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "accessible_count": accessible,
            "inaccessible_count": total - accessible,
            "total_count": total,
            "link_status": link_status
        }

        save_results(output)
        logging.info(f"共检查 {total} 个链接，成功 {accessible} 个，失败 {total - accessible} 个")
    except Exception as e:
        logging.exception(f"运行主程序失败: {e}")
        if 'output' in locals():
            save_results(output)

if __name__ == "__main__":
    main()
