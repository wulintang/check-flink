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

# 请求头配置（保持不变）
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

RAW_HEADERS = {  # 仅用于获取原始数据
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36 "
        "(check-flink/1.0; +https://github.com/willow-god/check-flink)"
    ),
    "X-Check-Flink": "1.0"
}

# ====================== 白名单配置 ======================
# 白名单支持完整URL或域名（如"https://example.com"或"example.com"）
WHITELIST = [
    "https://www.quji.org/"
]
# ======================================================

PROXY_URL_TEMPLATE = f"{os.getenv('PROXY_URL')}{{}}" if os.getenv("PROXY_URL") else None
SOURCE_URL = os.getenv("SOURCE_URL", "./link.csv")
RESULT_FILE = "./result.json"
api_request_queue = Queue()
api2_request_queue = Queue()

if PROXY_URL_TEMPLATE:
    logging.info("代理 URL 获取成功，代理协议: %s", PROXY_URL_TEMPLATE.split(":")[0])
else:
    logging.info("未提供代理 URL")

# ====================== 白名单检查函数 ======================
def is_in_whitelist(link):
    """判断链接是否在白名单中（支持完整URL或域名匹配）"""
    parsed = urlparse(link)
    domain = parsed.hostname or link  # 提取域名（如"baidu.com"）
    return (link in WHITELIST) or (domain in WHITELIST)
# ==========================================================

def check_ssl_for_accessibility(url):
    """SSL检测（所有链接都执行，包括白名单）"""
    parsed_url = urlparse(url)
    if parsed_url.scheme != "https":
        return True  # HTTP无需SSL检测
    hostname = parsed_url.hostname
    if not hostname:
        return False
    try:
        context = ssl.create_default_context()
        with socket.create_connection((hostname, 443), timeout=10) as sock:
            with context.wrap_socket(sock, server_hostname=hostname) as secure_sock:
                cert = secure_sock.getpeercert()
                expiry_date = datetime.strptime(cert['notAfter'], '%b %d %H:%M:%S %Y %Z')
                return expiry_date > datetime.now()
    except:
        return False

def request_url(session, url, headers=HEADERS, desc="", timeout=15, verify=True, **kwargs):
    """统一请求函数（所有链接都执行，包括白名单）"""
    try:
        start_time = time.time()
        response = session.get(url, headers=headers, timeout=timeout, verify=verify,** kwargs)
        latency = round(time.time() - start_time, 2)
        return response, latency
    except requests.RequestException as e:
        logging.warning(f"[{desc}] 请求失败: {url}，错误: {e}")
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
    # 白名单链接也执行完整检测（SSL+请求），保留真实结果
    item['raw_latency'] = -1  # 存储原始检测延迟（无论是否通过）
    item['raw_status'] = "未检测"  # 存储原始检测状态（用于日志）

    # 第一步：SSL检测（白名单也执行）
    ssl_ok = check_ssl_for_accessibility(link)
    if not ssl_ok:
        item['raw_status'] = "SSL异常"
        logging.warning(f"[SSL检测] {link} SSL配置错误或证书无效（白名单仍记录真实结果）")
    else:
        # 第二步：执行请求检测（直接访问+代理访问，白名单也执行）
        for method, url in [("直接访问", link), ("代理访问", PROXY_URL_TEMPLATE.format(link) if PROXY_URL_TEMPLATE else None)]:
            if not url or not is_url(url):
                continue
            response, latency = request_url(session, url, desc=method)
            if response and response.status_code == 200:
                item['raw_latency'] = latency
                item['raw_status'] = f"{method}成功（状态码200）"
                logging.info(f"[{method}] {link} 成功，延迟 {latency}s（白名单按真实结果记录）")
                return item, latency  # 返回真实延迟
            elif response:
                item['raw_status'] = f"{method}状态码异常（{response.status_code}）"
                logging.warning(f"[{method}] {link} 状态码异常（{response.status_code}，白名单仍记录）")
            else:
                item['raw_status'] = f"{method}请求失败"
                logging.warning(f"[{method}] {link} 请求失败（白名单仍记录）")

        # 第三步：API检查（白名单也执行）
        api_request_queue.put(item)
        return item, -1  # 先按非白名单逻辑返回，后续处理时修正

    return item, -1  # SSL异常时先返回-1，后续处理时修正

def handle_api1_requests(session):
    """API1检查（白名单也执行）"""
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
                    item['raw_http_code'] = http_code
                    item['raw_latency'] = latency
                    item['raw_status'] = f"API1检查通过（状态码{http_code}）"
                    logging.info(f"[API1] {link} 状态码 {http_code}（白名单按真实结果记录）")
                    results.append(item)
                    continue
                else:
                    item['raw_status'] = f"API1调用失败（错误码{res_json.get('code')}）"
                    logging.warning(f"[API1] {link} 调用失败（白名单仍记录）")
            except Exception as e:
                item['raw_status'] = f"API1解析失败（{e}）"
                logging.error(f"[API1] {link} 解析失败（白名单仍记录）")
        else:
            item['raw_status'] = "API1请求失败"
            logging.warning(f"[API1] {link} 请求失败（白名单仍记录）")
            
        api2_request_queue.put(item)
    
    return results

def handle_api2_requests(session):
    """API2检查（白名单也执行）"""
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
                    item['raw_latency'] = latency
                    item['raw_status'] = "API2检查通过（状态码200）"
                    logging.info(f"[API2] {link} 成功（白名单按真实结果记录）")
                else:
                    item['raw_status'] = f"API2状态异常（{res_json.get('code')},{res_json.get('data')}）"
                    logging.warning(f"[API2] {link} 状态异常（白名单仍记录）")
            except Exception as e:
                item['raw_status'] = f"API2解析失败（{e}）"
                logging.error(f"[API2] {link} 解析失败（白名单仍记录）")
        else:
            item['raw_status'] = "API2请求失败"
            logging.warning(f"[API2] {link} 请求失败（白名单仍记录）")
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
                        results[idx] = (item, updated_item.get('raw_latency', -1))
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

                # 获取原始检测数据（无论是否白名单，都保留）
                raw_latency = item.get('raw_latency', latency)  # 真实延迟
                raw_status = item.get('raw_status', "未知状态")  # 真实检测状态

                # 判断是否为白名单
                in_whitelist = is_in_whitelist(link)

                # 白名单逻辑：无论原始结果如何，都视为通过（失败计数归0）
                # 非白名单：按原始结果判断
                prev_entry = next((x for x in previous_results.get("link_status", []) if x.get("link") == link), {})
                prev_fail_count = prev_entry.get("fail_count", 0)
                
                if in_whitelist:
                    # 白名单：强制视为通过，失败计数0，保留真实延迟
                    final_latency = raw_latency  # 保留真实延迟
                    final_fail_count = 0
                    logging.info(f"[白名单处理] {link} 实际状态: {raw_status}，强制标记为通过")
                else:
                    # 非白名单：按原始结果判断
                    final_latency = raw_latency if raw_latency != -1 else -1
                    final_fail_count = prev_fail_count + 1 if final_latency == -1 else 0

                link_status.append({
                    'name': name,
                    'link': link,
                    'latency': final_latency,  # 真实延迟（白名单也保留）
                    'fail_count': final_fail_count,  # 白名单强制为0
                    'raw_status': raw_status,  # 新增：记录真实检测状态（方便排查）
                    'is_whitelist': in_whitelist  # 新增：标记是否为白名单
                })
            except Exception as e:
                logging.error(f"处理链接时发生错误: {item}, 错误: {e}")

        link_status = [entry for entry in link_status if entry["link"] in current_links]

        # 统计可访问数量（白名单无论结果都算可访问）
        accessible = sum(1 for x in link_status if (x["is_whitelist"]) or (x["latency"] != -1))
        total = len(link_status)
        output = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "accessible_count": accessible,
            "inaccessible_count": total - accessible,
            "total_count": total,
            "link_status": link_status
        }

        save_results(output)
        logging.info(f"共检查 {total} 个链接，成功 {accessible} 个（含白名单强制通过），失败 {total - accessible} 个")
        logging.info(f"结果已保存至: ./result.json")
    except Exception as e:
        logging.exception(f"运行主程序失败: {e}")

if __name__ == "__main__":
    main()
