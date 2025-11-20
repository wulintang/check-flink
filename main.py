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

# 请求头配置
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

RAW_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36 "
        "(check-flink/1.0; +https://github.com/willow-god/check-flink)"
    ),
    "X-Check-Flink": "1.0"
}

# 白名单配置
WHITELIST = [
    "https://www.quji.org/",
    "https://www.gymxbl.com/",
    "https://www.52txr.cn/"
]

PROXY_URL_TEMPLATE = f"{os.getenv('PROXY_URL')}{{}}" if os.getenv("PROXY_URL") else None
SOURCE_URL = os.getenv("SOURCE_URL", "./link.csv")
RESULT_FILE = "./result.json"
api1_queue = Queue()
api2_queue = Queue()

if PROXY_URL_TEMPLATE:
    logging.info("代理 URL 获取成功，代理协议: %s", PROXY_URL_TEMPLATE.split(":")[0])
else:
    logging.info("未提供代理 URL")

def is_in_whitelist(link):
    parsed = urlparse(link)
    domain = parsed.hostname or link
    return (link in WHITELIST) or (domain in WHITELIST)

def check_ssl_for_accessibility(url):
    """SSL检测：返回（是否正常，错误信息，耗时，是否属于证书有效性问题）"""
    start_time = time.time()
    parsed_url = urlparse(url)
    if parsed_url.scheme != "https":
        latency = round(time.time() - start_time, 2)
        return (True, "非HTTPS链接，无需SSL检测", latency, False)
    
    hostname = parsed_url.hostname
    if not hostname:
        latency = round(time.time() - start_time, 2)
        return (False, "主机名解析失败（非证书问题）", latency, False)
    
    try:
        context = ssl.create_default_context()
        with socket.create_connection((hostname, 443), timeout=10) as sock:
            with context.wrap_socket(sock, server_hostname=hostname) as secure_sock:
                cert = secure_sock.getpeercert()
                expiry_date = datetime.strptime(cert['notAfter'], '%b %d %H:%M:%S %Y %Z')
                latency = round(time.time() - start_time, 2)
                if expiry_date > datetime.now():
                    return (True, f"SSL证书有效（到期时间: {expiry_date.strftime('%Y-%m-%d')}）", latency, False)
                else:
                    # 证书到期：属于证书有效性问题
                    return (False, f"SSL证书已到期（到期时间: {expiry_date.strftime('%Y-%m-%d')}）", latency, True)
    except ssl.CertificateError:
        # 证书无效（包括域名不匹配、不被信任等）：属于证书有效性问题
        latency = round(time.time() - start_time, 2)
        return (False, "SSL证书无效（域名不匹配/不被信任）", latency, True)
    except socket.timeout:
        # 超时：属于网络问题，非证书有效性问题
        latency = round(time.time() - start_time, 2)
        return (False, "SSL连接超时（网络问题）", latency, False)
    except Exception as e:
        # 其他错误：区分是否为证书相关
        if "certificate" in str(e).lower() or "ssl" in str(e).lower():
            # 明确提到证书/SSL的错误：属于证书有效性问题
            latency = round(time.time() - start_time, 2)
            return (False, f"SSL证书错误: {str(e)}", latency, True)
        else:
            # 其他网络错误（如不可达）：非证书问题
            latency = round(time.time() - start_time, 2)
            return (False, f"网络连接失败: {str(e)}", latency, False)

def request_url(session, url, headers=HEADERS, desc="", timeout=15, verify=True):
    """统一请求函数"""
    try:
        start_time = time.time()
        response = session.get(url, headers=headers, timeout=timeout, verify=verify)
        latency = round(time.time() - start_time, 2)
        return response, latency, response.status_code
    except requests.RequestException as e:
        latency = round(time.time() - start_time, 2)
        logging.warning(f"[{desc}] 请求失败: {url}，错误: {e}，耗时 {latency}s")
        return None, latency, -1

def is_success_status_code(status_code):
    return status_code in (200, 301, 302)

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
                response, _, _ = request_url(session, origin_path, headers=RAW_HEADERS, desc="数据源")
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
            return data['link_list']
        elif isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass

    try:
        rows = list(csv.reader(content.splitlines()))
        return [{'name': row[0], 'link': row[1]} for row in rows if len(row) == 2]
    except Exception as e:
        logging.error(f"CSV 解析失败: {e}")
        return []

def check_direct_and_proxy(item, session):
    link = item['link']
    item['check_layer'] = "未通过任何检测"
    item['raw_status_code'] = -1
    total_latency = 0.0
    
    # 白名单判断
    whitelist_check_start = time.time()
    item['is_whitelist'] = is_in_whitelist(link)
    whitelist_latency = round(time.time() - whitelist_check_start, 2)
    total_latency += whitelist_latency
    item['whitelist_check_latency'] = whitelist_latency

    # SSL检测（核心修复）
    parsed_url = urlparse(link)
    ssl_ok, ssl_msg, ssl_latency, is_cert_invalid = check_ssl_for_accessibility(link)
    item['ssl_ok'] = ssl_ok
    item['ssl_message'] = ssl_msg
    item['is_cert_invalid'] = is_cert_invalid  # 标记是否属于证书有效性问题
    total_latency = round(total_latency + ssl_latency, 2)
    
    # 关键修复：所有证书有效性问题（包括到期、域名不匹配、不被信任等）→ 直接判定不可访问
    if is_cert_invalid:
        logging.error(f"[SSL证书问题] {link} → {ssl_msg}，直接判定为不可访问，终止后续检测")
        item['is_accessible'] = False
        item['check_layer'] = "SSL证书无效"
        return item, total_latency

    # 只有证书有效/非HTTPS链接，才继续检测可访问性
    logging.info(f"[证书正常] {link}（{ssl_msg}），继续检测可访问性")

    # 直接访问检测
    response, direct_latency, status_code = request_url(session, link, desc="直接访问")
    total_latency = round(total_latency + direct_latency, 2)
    
    if is_success_status_code(status_code):
        logging.info(f"[直接访问] {link} 成功（状态码: {status_code}）")
        item['check_layer'] = "直接访问"
        item['raw_status_code'] = status_code
        item['is_accessible'] = True
        return item, total_latency

    # 代理访问检测
    item['raw_status_code'] = status_code
    logging.warning(f"[直接访问] {link} 失败（状态码: {status_code}），尝试代理访问")
    
    if PROXY_URL_TEMPLATE:
        proxy_url = PROXY_URL_TEMPLATE.format(link)
        response, proxy_latency, status_code = request_url(session, proxy_url, desc="代理访问")
        total_latency = round(total_latency + proxy_latency, 2)
        
        if is_success_status_code(status_code):
            logging.info(f"[代理访问] {link} 成功（状态码: {status_code}）")
            item['check_layer'] = "代理访问"
            item['raw_status_code'] = status_code
            item['is_accessible'] = True
            return item, total_latency
        
        item['raw_status_code'] = status_code
        logging.warning(f"[代理访问] {link} 失败（状态码: {status_code}），进入API检测")

    # 进入API检测队列
    item['current_latency'] = total_latency
    api1_queue.put(item)
    
    return item, total_latency

def handle_api1():
    with requests.Session() as session:
        results = []
        while not api1_queue.empty():
            item = api1_queue.get()
            link = item['link']
            total_latency = item.get('current_latency', 0.0)
            
            api_url = f"https://v.api.aa1.cn/api/httpcode/?url={link}"
            response, api1_latency, status_code = request_url(
                session, api_url, headers=RAW_HEADERS, desc="API1检测", timeout=30
            )
            total_latency = round(total_latency + api1_latency, 2)

            if status_code == 200:
                try:
                    res_json = response.json()
                    target_status = int(res_json.get("httpcode"))
                    if is_success_status_code(target_status):
                        logging.info(f"[API1检测] {link} 成功（目标状态码: {target_status}）")
                        item['check_layer'] = "API1检测"
                        item['raw_status_code'] = target_status
                        item['is_accessible'] = True
                        results.append((item, total_latency))
                        continue
                except Exception as e:
                    logging.error(f"[API1解析] {link} 失败: {e}")
            item['current_latency'] = total_latency
            api2_queue.put(item)
            results.append((item, total_latency))
        return results

def handle_api2():
    with requests.Session() as session:
        results = []
        while not api2_queue.empty():
            item = api2_queue.get()
            link = item['link']
            total_latency = item.get('current_latency', 0.0)
            
            api_url = f"https://v2.xxapi.cn/api/status?url={link}"
            response, api2_latency, status_code = request_url(
                session, api_url, headers=RAW_HEADERS, desc="API2检测", timeout=30
            )
            total_latency = round(total_latency + api2_latency, 2)

            if status_code == 200:
                try:
                    res_json = response.json()
                    target_status = int(res_json.get("data"))
                    if is_success_status_code(target_status):
                        logging.info(f"[API2检测] {link} 成功（目标状态码: {target_status}）")
                        item['check_layer'] = "API2检测"
                        item['raw_status_code'] = target_status
                        item['is_accessible'] = True
                        results.append((item, total_latency))
                        continue
                except Exception as e:
                    logging.error(f"[API2解析] {link} 失败: {e}")
            
            # API2检测失败，标记为不可访问
            item['is_accessible'] = False
            results.append((item, total_latency))
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
                initial_results = list(executor.map(lambda item: check_direct_and_proxy(item, session), link_list))

        api1_results = handle_api1()
        api2_results = handle_api2()

        result_map = {item['link']: (item, latency) for item, latency in initial_results}
        for item, latency in api1_results + api2_results:
            if item['check_layer'] in ["API1检测", "API2检测"]:
                result_map[item['link']] = (item, latency)
        final_results = list(result_map.values())

        current_links = {item['link'] for item in link_list}
        link_status = []

        for item, latency in final_results:
            try:
                name = item.get('name', '未知')
                link = item.get('link')
                if not link or link not in current_links:
                    continue

                in_whitelist = item.get('is_whitelist', False)
                ssl_ok = item.get('ssl_ok', False)
                ssl_message = item.get('ssl_message', "未检测")
                is_cert_invalid = item.get('is_cert_invalid', False)
                is_accessible = item.get('is_accessible', False)

                # 白名单链接强制标记为可访问
                if in_whitelist:
                    is_accessible = True
                    final_fail_count = 0
                else:
                    prev_entry = next((x for x in previous_results.get("link_status", []) if x.get("link") == link), {})
                    prev_fail_count = prev_entry.get("fail_count", 0)
                    final_fail_count = prev_fail_count + 1 if not is_accessible else 0

                final_latency = round(latency, 2)
                link_status.append({
                    'name': name,
                    'link': link,
                    'latency': final_latency,
                    'fail_count': final_fail_count,
                    'check_layer': item.get('check_layer', '未通过任何检测'),
                    'raw_status_code': item.get('raw_status_code', -1),
                    'ssl_ok': ssl_ok,
                    'ssl_message': ssl_message,
                    'is_cert_invalid': is_cert_invalid,
                    'is_whitelist': in_whitelist,
                    'is_accessible': is_accessible
                })
            except Exception as e:
                logging.error(f"处理结果时出错: {item}, 错误: {e}")

        total = len(link_status)
        accessible = sum(1 for x in link_status if x['is_accessible'])
        output = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "accessible_count": accessible,
            "inaccessible_count": total - accessible,
            "total_count": total,
            "link_status": link_status
        }

        save_results(output)
        logging.info(f"检测完成：共 {total} 个链接，可访问 {accessible} 个（含白名单 {sum(1 for x in link_status if x['is_whitelist'])} 个），不可访问 {total - accessible} 个")
        logging.info(f"结果已保存至: {RESULT_FILE}")
    except Exception as e:
        logging.exception(f"主程序运行失败: {e}")

if __name__ == "__main__":
    main()
    
