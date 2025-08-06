import os
import csv
import json
import time
import random
import logging
import requests
import warnings
from queue import Queue
from datetime import datetime
from urllib.parse import urlparse, quote
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

# 检查代理URL模板格式
PROXY_URL_TEMPLATE = os.getenv('PROXY_URL') if os.getenv("PROXY_URL") else None
if PROXY_URL_TEMPLATE and "{}" not in PROXY_URL_TEMPLATE:
    logging.warning("代理 URL 模板缺少占位符 '{}'，将忽略代理")
    PROXY_URL_TEMPLATE = None
elif PROXY_URL_TEMPLATE:
    PROXY_URL_TEMPLATE = f"{PROXY_URL_TEMPLATE}{{}}"

SOURCE_URL = os.getenv("SOURCE_URL", "./link.csv")  # 默认本地文件
RESULT_FILE = "./result.json"
api_request_queue = Queue()

if PROXY_URL_TEMPLATE:
    logging.info("代理 URL 获取成功，代理协议: %s", PROXY_URL_TEMPLATE.split(":")[0])
else:
    logging.info("未提供代理 URL")

def request_url(session, url, headers=HEADERS, desc="", timeout=15, verify=True, **kwargs):
    """统一封装的 GET 请求函数"""
    try:
        start_time = time.time()
        response = session.get(url, headers=headers, timeout=timeout, verify=verify,** kwargs)
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
    for method, url in [("直接访问", link), ("代理访问", PROXY_URL_TEMPLATE.format(link) if PROXY_URL_TEMPLATE else None)]:
        if not url or not is_url(url):
            logging.warning(f"[{method}] 无效链接: {link}")
            continue
        response, latency = request_url(session, url, desc=method)
        if response and response.status_code == 200:
            logging.info(f"[{method}] 成功访问: {link} ，延迟 {latency} 秒")
            return item, latency
        elif response and response.status_code != 200:
            logging.warning(f"[{method}] 状态码异常: {link} -> {response.status_code}")
        else:
            logging.warning(f"[{method}] 请求失败，Response 无效: {link}")

    api_request_queue.put(item)
    return item, -1

def human_delay():
    """生成随机延迟（1-3秒）模拟人类操作间隔"""
    return random.uniform(1, 3)

def handle_api_requests():
    results = []
    with requests.Session() as session:
        logging.info(f"开始处理API检查队列，队列大小: {api_request_queue.qsize()}")
        while not api_request_queue.empty():
            time.sleep(human_delay())  # 人类操作间隔
            item = api_request_queue.get()
            link = item['link']
            # 对URL进行编码处理
            api_url = f"https://v2.xxapi.cn/api/status?url={quote(link)}"
            
            response, latency = request_url(session, api_url, desc="API检查")
            if response:
                # 只根据HTTP状态码判断，忽略响应内容
                if response.status_code == 200:
                    logging.info(f"[API] 确认可访问: {link} (HTTP状态码: 200)")
                    results.append((item, latency))
                else:
                    logging.warning(f"[API] 不可访问: {link} (HTTP状态码: {response.status_code})")
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

        with requests.Session() as session:
            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                results = list(executor.map(lambda item: check_link(item, session), link_list))

            # 修复：调用handle_api_requests时不传递session参数
            updated_api_results = handle_api_requests()
            # 修复：正确处理元组类型的updated_api_results
            for updated_item in updated_api_results:
                updated_link = updated_item[0]['link']
                updated_latency = updated_item[1]
                for idx, (item, latency) in enumerate(results):
                    if item['link'] == updated_link:
                        results[idx] = (item, updated_latency)
                        break

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
        logging.info(f"结果已保存至: {RESULT_FILE}")
    except Exception as e:
        logging.exception(f"运行主程序失败: {e}")

if __name__ == "__main__":
    main()
