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

PROXY_URL_TEMPLATE = f"{os.getenv('PROXY_URL')}{{}}" if os.getenv("PROXY_URL") else None
SOURCE_URL = os.getenv("SOURCE_URL", "./link.csv")  # 默认本地文件
RESULT_FILE = "./result.json"
api_request_queue = Queue()
api2_request_queue = Queue()

if PROXY_URL_TEMPLATE:
    logging.info("代理 URL 获取成功，代理协议: %s", PROXY_URL_TEMPLATE.split(":")[0])
else:
    logging.info("未提供代理 URL")

def check_ssl_for_accessibility(url):
    """SSL检测：判断是否因SSL问题导致不可访问，返回元组(状态, 错误信息)"""
    parsed_url = urlparse(url)
    if parsed_url.scheme != "https":
        # HTTP链接无需SSL检测，直接返回正常
        return (True, None)
    
    hostname = parsed_url.hostname
    if not hostname:
        return (False, "主机名解析失败")  # 主机名解析失败
    
    try:
        # 尝试建立SSL连接
        context = ssl.create_default_context()
        with socket.create_connection((hostname, 443), timeout=10) as sock:
            with context.wrap_socket(sock, server_hostname=hostname) as secure_sock:
                # 检查证书是否过期
                cert = secure_sock.getpeercert()
                expiry_date = datetime.strptime(cert['notAfter'], '%b %d %H:%M:%S %Y %Z')
                if expiry_date > datetime.now():
                    return (True, None)
                else:
                    return (False, "SSL证书已过期")
    except ssl.CertificateError:
        return (False, "SSL证书验证失败")
    except ssl.SSLError as e:
        return (False, f"SSL错误: {str(e)}")
    except socket.timeout:
        return (False, "SSL连接超时")
    except Exception as e:
        return (False, f"SSL相关错误: {str(e)}")

def request_url(session, url, headers=HEADERS, desc="", timeout=15, verify=True, **kwargs):
    """统一封装的 GET 请求函数，返回(响应, 延迟, 错误信息)"""
    try:
        start_time = time.time()
        response = session.get(url, headers=headers, timeout=timeout, verify=verify,** kwargs)
        latency = round(time.time() - start_time, 2)
        return (response, latency, None)
    except requests.Timeout:
        return (None, -1, "请求超时")
    except requests.ConnectionError:
        return (None, -1, "连接错误")
    except requests.TooManyRedirects:
        return (None, -1, "重定向次数过多")
    except requests.RequestException as e:
        error_msg = f"请求异常: {str(e)}"
        logging.warning(f"[{desc}] 请求失败: {url}，错误: {error_msg}")
        return (None, -1, error_msg)

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
                response, _, error = request_url(session, origin_path, headers=RAW_HEADERS, desc="数据源")
                if error:
                    logging.error(f"获取远程数据源失败: {error}")
                    return []
                content = response.text if response else ""
        else:
            with open(origin_path, "r", encoding="utf-8") as f:
                content = f.read()
    except Exception as e:
        logging.error(f"读取数据失败: {str(e)}")
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
        logging.error(f"CSV 解析失败: {str(e)}")
        return []

def check_link(item, session):
    link = item['link']
    error_messages = []  # 收集所有错误信息
    
    # 第一步：SSL检测（仅HTTPS链接）
    ssl_ok, ssl_error = check_ssl_for_accessibility(link)
    if not ssl_ok and ssl_error:
        error_messages.append(f"SSL检测: {ssl_error}")
        logging.warning(f"[SSL检测] {ssl_error}，链接不可访问: {link}")
        return (item, -1, "; ".join(error_messages))
    
    # 后续检测流程
    methods = [("直接访问", link)]
    if PROXY_URL_TEMPLATE:
        methods.append(("代理访问", PROXY_URL_TEMPLATE.format(link)))
    
    for method, url in methods:
        if not url or not is_url(url):
            error = f"{method}: 无效链接"
            error_messages.append(error)
            logging.warning(f"{error}: {link}")
            continue
            
        response, latency, error = request_url(session, url, desc=method)
        if error:
            error_messages.append(f"{method}: {error}")
        elif response:
            if response.status_code == 200:
                logging.info(f"[{method}] 成功访问: {link} ，延迟 {latency} 秒")
                return (item, latency, None)
            else:
                error = f"{method}: 状态码异常 ({response.status_code})"
                error_messages.append(error)
                logging.warning(f"{error}: {link}")
        else:
            error = f"{method}: 未知错误"
            error_messages.append(error)

    # 如果所有方法都失败，加入API检查队列
    api_request_queue.put((item, error_messages))
    return (item, -1, "; ".join(error_messages))

def handle_api1_requests(session):
    """第一个API检查处理函数"""
    results = []
    while not api_request_queue.empty():
        time.sleep(0.2)
        item, prev_errors = api_request_queue.get()
        link = item['link']
        api_url = f"https://v.api.aa1.cn/api/httpcode/?url={link}"
        response, latency, error = request_url(session, api_url, headers=RAW_HEADERS, desc="API1 检查", timeout=30)
        
        if error:
            prev_errors.append(f"API1检查: {error}")
            api2_request_queue.put((item, prev_errors))
            continue
            
        if response:
            try:
                res_json = response.json()
                if int(res_json.get("code")) == 200:
                    http_code = res_json.get("httpcode")
                    item['http_code'] = http_code
                    item['latency'] = latency
                    if int(http_code) == 200:
                        logging.info(f"[API1] 访问 {link} ，状态码 {http_code}")
                        results.append((item, latency, None))
                        continue
                    else:
                        error = f"API1检查: 状态码异常 ({http_code})"
                        prev_errors.append(error)
                else:
                    error = f"API1检查: 调用失败 (错误码 {res_json.get('code')})"
                    prev_errors.append(error)
            except Exception as e:
                error = f"API1检查: 解析响应失败 ({str(e)})"
                prev_errors.append(error)
                logging.error(error)
        
        api2_request_queue.put((item, prev_errors))
    
    return results

def handle_api2_requests(session):
    """第二个API检查处理函数"""
    results = []
    while not api2_request_queue.empty():
        time.sleep(0.2)
        item, prev_errors = api2_request_queue.get()
        link = item['link']
        api_url = f"https://v2.xxapi.cn/api/status?url={link}"
        response, latency, error = request_url(session, api_url, headers=RAW_HEADERS, desc="API2 检查", timeout=30)
        
        if error:
            prev_errors.append(f"API2检查: {error}")
        elif response:
            try:
                res_json = response.json()
                if int(res_json.get("code")) == 200 and int(res_json.get("data")) == 200:
                    logging.info(f"[API2] 成功访问: {link} ，状态码 200")
                    results.append((item, latency, None))
                    continue
                else:
                    error = f"API2检查: 状态异常 ({res_json.get('code')}, {res_json.get('data')})"
                    prev_errors.append(error)
            except Exception as e:
                error = f"API2检查: 解析响应失败 ({str(e)})"
                prev_errors.append(error)
        
        results.append((item, -1, "; ".join(prev_errors)))
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
            
            # 更新结果列表
            for updated_item, latency, error in api1_results + api2_results:
                for idx, (item, _, _) in enumerate(results):
                    if item['link'] == updated_item['link']:
                        results[idx] = (item, latency, error)
                        break

        current_links = {item['link'] for item in link_list}
        link_status = []

        for item, latency, error in results:
            try:
                name = item.get('name', '未知')
                link = item.get('link')
                if not link:
                    logging.warning(f"跳过无效项: {item}")
                    continue

                # 查找之前的记录
                prev_entry = next(
                    (x for x in previous_results.get("link_status", []) if x.get("link") == link), 
                    {}
                )
                prev_fail_count = prev_entry.get("fail_count", 0)
                fail_count = prev_fail_count + 1 if latency == -1 else 0

                link_status.append({
                    'name': name,
                    'link': link,
                    'latency': latency,
                    'fail_count': fail_count,
                    'error': error  # 新增错误信息字段
                })
            except Exception as e:
                logging.error(f"处理链接时发生错误: {item}, 错误: {str(e)}")

        # 过滤掉不在当前链接列表中的记录
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
        logging.exception(f"运行主程序失败: {str(e)}")

if __name__ == "__main__":
    main()
