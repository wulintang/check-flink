import os
import csv
import json
import time
import socket
import ssl
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

# ====================== 新增：白名单配置 ======================
# 白名单支持完整URL或域名（如"https://example.com"或"example.com"）
WHITELIST = [
    "https://www.quji.org/",
]
# ============================================================

PROXY_URL_TEMPLATE = f"{os.getenv('PROXY_URL')}{{}}" if os.getenv("PROXY_URL") else None
SOURCE_URL = os.getenv("SOURCE_URL", "./link.csv")  # 默认本地文件
RESULT_FILE = "./result.json"
api_request_queue = Queue()
api2_request_queue = Queue()

if PROXY_URL_TEMPLATE:
    logging.info("代理 URL 获取成功，代理协议: %s", PROXY_URL_TEMPLATE.split(":")[0])
else:
    logging.info("未提供代理 URL")

# ====================== 新增：白名单检查函数 ======================
def is_in_whitelist(link):
    """判断链接是否在白名单中（支持完整URL或域名匹配）"""
    parsed = urlparse(link)
    # 提取链接的域名（如"https://www.baidu.com/path"提取为"baidu.com"）
    domain = parsed.hostname or link
    # 检查完整URL或域名是否在白名单中
    return (link in WHITELIST) or (domain in WHITELIST)
# ================================================================

def check_ssl_for_accessibility(url):
    """SSL检测：仅判断是否因SSL问题导致不可访问，返回True（SSL正常）/False（SSL异常）"""
    parsed_url = urlparse(url)
    if parsed_url.scheme != "https":
        # HTTP链接无需SSL检测，直接返回正常
        return True
    
    hostname = parsed_url.hostname
    if not hostname:
        return False  # 主机名解析失败，视为不可访问
    
    try:
        # 尝试建立SSL连接（仅验证SSL有效性，不获取完整内容）
        context = ssl.create_default_context()
        with socket.create_connection((hostname, 443), timeout=10) as sock:
            with context.wrap_socket(sock, server_hostname=hostname) as secure_sock:
                # 检查证书是否过期
                cert = secure_sock.getpeercert()
                expiry_date = datetime.strptime(cert['notAfter'], '%b %d %H:%M:%S %Y %Z')
                return expiry_date > datetime.now()  # 证书未过期则正常
    except:
        # 任何SSL相关错误（证书无效、过期、不匹配等）均视为不可访问
        return False

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
    
    # ====================== 新增：白名单优先判断 ======================
    if is_in_whitelist(link):
        logging.info(f"[白名单] 链接直接通过: {link}")
        return item, 0.0  # 0.0表示白名单直接通过（区别于正常延迟）
    # =================================================================
    
    # 第一步：SSL检测（仅HTTPS链接），若SSL异常直接判定为失败
    if not check_ssl_for_accessibility(link):
        logging.warning(f"[SSL检测] SSL配置错误或证书无效，链接不可访问: {link}")
        return item, -1  # 直接返回失败
    
    # 后续检测流程（保持原始逻辑：直接访问、代理访问、API检查）
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

def handle_api1_requests(session):
    """第一个API检查处理函数"""
    results = []
    while not api_request_queue.empty():
        time.sleep(0.2)
        item = api_request_queue.get()
        link = item['link']
        api_url = f"https://v.api.aa1.cn/api/httpcode/?url={link}"
        response, latency = request_url(session, api_url, headers=RAW_HEADERS, desc="API1 检查", timeout=30)
        
        if response:
            try:
                res_json = response.json()
                if int(res_json.get("code")) == 200:
                    http_code = res_json.get("httpcode")
                    item['http_code'] = http_code
                    item['latency'] = latency
                    logging.info(f"[API1] 访问 {link} ，状态码 {http_code}")
                    results.append(item)
                    continue
                else:
                    logging.warning(f"[API1] 调用失败: {link} -> 错误码 {res_json.get('code')}")
            except Exception as e:
                logging.error(f"[API1] 解析响应失败: {link}，错误: {e}")
        else:
            logging.warning(f"[API1] 请求失败: {link}")
            
        api2_request_queue.put(item)
    
    return results

def handle_api2_requests(session):
    """第二个API检查处理函数"""
    results = []
    while not api2_request_queue.empty():
        time.sleep(0.2)
        item = api2_request_queue.get()
        link = item['link']
        api_url = f"https://v2.xxapi.cn/api/status?url={link}"
        response, latency = request_url(session, api_url, headers=RAW_HEADERS, desc="API2 检查", timeout=30)
        if response:
            try:
                res_json = response.json()
                if int(res_json.get("code")) == 200 and int(res_json.get("data")) == 200:
                    logging.info(f"[API2] 成功访问: {link} ，状态码 200")
                    item['latency'] = latency
                else:
                    logging.warning(f"[API2] 状态异常: {link} -> [{res_json.get('code')}, {res_json.get('data')}]")
                    item['latency'] = -1
            except Exception as e:
                logging.error(f"[API2] 解析响应失败: {link}，错误: {e}")
                item['latency'] = -1
        else:
            item['latency'] = -1
        results.append(item)
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

            api1_results = handle_api1_requests(session)
            api2_results = handle_api2_requests(session)
            all_api_results = api1_results + api2_results
            
            for updated_item in all_api_results:
                for idx, (item, latency) in enumerate(results):
                    if item['link'] == updated_item['link']:
                        results[idx] = (item, updated_item['latency'])
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
                # 白名单链接失败次数始终为0
                fail_count = 0 if latency == 0.0 else (prev_fail_count + 1 if latency == -1 else 0)

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
        logging.info(f"结果已保存至: ./result.json")
    except Exception as e:
        logging.exception(f"运行主程序失败: {e}")

if __name__ == "__main__":
    main()
