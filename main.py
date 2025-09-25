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

# -------------------------- 基础配置 --------------------------
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

# 白名单、路径、队列配置
WHITELIST = ["https://www.quji.org/", "https://www.gymxbl.com/"]
PROXY_URL_TEMPLATE = f"{os.getenv('PROXY_URL')}{{}}" if os.getenv("PROXY_URL") else None
SOURCE_URL = os.getenv("SOURCE_URL", "./link.csv")
RESULT_FILE = "./result.json"
api1_queue = Queue()
api2_queue = Queue()

# 代理信息日志
if PROXY_URL_TEMPLATE:
    logging.info("代理配置生效，协议: %s", PROXY_URL_TEMPLATE.split(":")[0])
else:
    logging.info("未配置代理，跳过代理访问测试")


# -------------------------- 工具函数 --------------------------
def is_in_whitelist(link):
    """判断链接是否在白名单内（支持域名/完整链接匹配）"""
    parsed = urlparse(link)
    domain = parsed.hostname or link
    return (link in WHITELIST) or (domain in WHITELIST)


def check_ssl_for_accessibility(url):
    """
    SSL证书检测（仅对HTTPS链接生效）
    返回：(ssl是否正常, 检测信息, 耗时)
    """
    start_time = time.time()
    parsed_url = urlparse(url)
    
    # 非HTTPS链接：无需SSL检测
    if parsed_url.scheme != "https":
        latency = round(time.time() - start_time, 2)
        return (True, "非HTTPS链接，无需SSL检测", latency)
    
    # HTTPS链接：执行证书检测
    hostname = parsed_url.hostname
    if not hostname:
        latency = round(time.time() - start_time, 2)
        return (False, "主机名解析失败", latency)
    
    try:
        context = ssl.create_default_context()
        with socket.create_connection((hostname, 443), timeout=10) as sock:
            with context.wrap_socket(sock, server_hostname=hostname) as secure_sock:
                cert = secure_sock.getpeercert()
                expiry_date = datetime.strptime(cert['notAfter'], '%b %d %H:%M:%S %Y %Z')
                latency = round(time.time() - start_time, 2)
                if expiry_date > datetime.now():
                    return (True, f"SSL证书有效（过期时间: {expiry_date.strftime('%Y-%m-%d %H:%M:%S')}）", latency)
                else:
                    return (False, f"SSL证书已过期（过期时间: {expiry_date.strftime('%Y-%m-%d %H:%M:%S')}）", latency)
    except ssl.CertificateError:
        latency = round(time.time() - start_time, 2)
        return (False, "SSL证书无效（不被信任/域名不匹配）", latency)
    except socket.timeout:
        latency = round(time.time() - start_time, 2)
        return (False, "SSL连接超时", latency)
    except Exception as e:
        latency = round(time.time() - start_time, 2)
        return (False, f"SSL连接失败: {str(e)[:50]}", latency)


def request_url(session, url, headers=HEADERS, desc="", timeout=15, verify=True):
    """统一HTTP请求函数，返回(响应对象, 耗时, 状态码)"""
    try:
        start_time = time.time()
        response = session.get(url, headers=headers, timeout=timeout, verify=verify)
        latency = round(time.time() - start_time, 2)
        return (response, latency, response.status_code)
    except requests.RequestException as e:
        latency = round(time.time() - start_time, 2)
        logging.warning(f"[{desc}] 失败: {url} → 错误: {str(e)[:40]}, 耗时 {latency}s")
        return (None, latency, -1)


def is_success_status_code(status_code):
    """判断状态码是否为成功（200正常/301-302重定向）"""
    return status_code in (200, 301, 302)


def load_previous_results():
    """加载历史结果（用于累计失败次数）"""
    if os.path.exists(RESULT_FILE):
        try:
            with open(RESULT_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            logging.warning(f"{RESULT_FILE} 解析错误，重新创建")
    return {}


def save_results(data):
    """保存最终结果到JSON文件"""
    with open(RESULT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)


def is_url(path):
    """判断路径是否为URL（区分本地文件和远程链接）"""
    return urlparse(path).scheme in ("http", "https")


def fetch_origin_data(origin_path):
    """读取数据源（支持JSON/CSV、本地/远程）"""
    logging.info(f"读取数据源: {origin_path}")
    # 读取数据源内容
    try:
        if is_url(origin_path):
            with requests.Session() as session:
                resp, _, status = request_url(session, origin_path, RAW_HEADERS, "数据源请求", 30)
                if status != 200 or not resp:
                    logging.error(f"远程数据源失败，状态码: {status}")
                    return []
                content = resp.text
        else:
            if not os.path.exists(origin_path):
                logging.error(f"本地文件不存在: {origin_path}")
                return []
            with open(origin_path, "r", encoding="utf-8") as f:
                content = f.read()
    except Exception as e:
        logging.error(f"读取失败: {str(e)}")
        return []
    
    # 解析JSON
    try:
        data = json.loads(content)
        if isinstance(data, dict) and "link_list" in data:
            link_list = data["link_list"]
            logging.info(f"JSON解析成功（link_list），共 {len(link_list)} 个链接")
            return link_list
        elif isinstance(data, list):
            logging.info(f"JSON解析成功（列表），共 {len(data)} 个链接")
            return data
        else:
            logging.warning("JSON格式不支持，尝试CSV")
    except json.JSONDecodeError:
        logging.warning("JSON解析失败，尝试CSV")
    
    # 解析CSV
    try:
        rows = list(csv.reader(content.splitlines()))
        link_list = [
            {"name": row[0].strip(), "link": row[1].strip()}
            for row in rows
            if len(row) >= 2 and row[1].strip().startswith(("http://", "https://"))
        ]
        logging.info(f"CSV解析成功，共 {len(link_list)} 个有效链接")
        return link_list
    except Exception as e:
        logging.error(f"CSV解析失败: {str(e)}")
        return []


# -------------------------- 核心检测逻辑 --------------------------
def check_direct_and_proxy(item, session):
    """
    核心检测流程：白名单判断 → SSL检测（HTTPS专属）→ 直接访问 → 代理访问 → 加入API1队列
    关键规则：HTTPS证书无效时，直接终止后续所有访问测试
    """
    link = item["link"]
    item["check_layer"] = "未通过任何检测"  # 记录成功的检测层级
    item["raw_status_code"] = -1              # 记录最终状态码
    total_latency = 0.0                       # 累计总耗时

    # 1. 白名单判断（仅记录，不影响流程）
    whitelist_start = time.time()
    item["is_whitelist"] = is_in_whitelist(link)
    whitelist_latency = round(time.time() - whitelist_start, 2)
    total_latency += whitelist_latency
    item["whitelist_check_latency"] = whitelist_latency

    # 2. SSL检测（HTTPS无效则终止后续流程）
    ssl_ok, ssl_msg, ssl_latency = check_ssl_for_accessibility(link)
    item["ssl_ok"] = ssl_ok
    item["ssl_message"] = ssl_msg
    total_latency = round(total_latency + ssl_latency, 2)

    parsed_url = urlparse(link)
    if parsed_url.scheme == "https" and not ssl_ok:
        logging.error(f"[SSL终止] {link} → {ssl_msg}，耗时 {total_latency}s，不继续访问")
        return (item, total_latency)
    elif parsed_url.scheme == "https":
        logging.info(f"[SSL通过] {link} → {ssl_msg}，继续直接访问")
    else:
        logging.info(f"[HTTP链接] {link}，跳过SSL直接访问")

    # 3. 直接访问测试
    direct_resp, direct_latency, direct_status = request_url(session, link, "直接访问", 20)
    total_latency = round(total_latency + direct_latency, 2)
    if is_success_status_code(direct_status):
        logging.info(f"[直接成功] {link} → 状态码 {direct_status}，耗时 {total_latency}s")
        item["check_layer"] = "直接访问"
        item["raw_status_code"] = direct_status
        return (item, total_latency)
    item["raw_status_code"] = direct_status
    logging.warning(f"[直接失败] {link} → 状态码 {direct_status}，尝试代理")

    # 4. 代理访问测试（有代理才执行）
    if PROXY_URL_TEMPLATE:
        proxy_url = PROXY_URL_TEMPLATE.format(link)
        proxy_resp, proxy_latency, proxy_status = request_url(
            session, proxy_url, "代理访问", 25, verify=False
        )
        total_latency = round(total_latency + proxy_latency, 2)
        if is_success_status_code(proxy_status):
            logging.info(f"[代理成功] {link} → 状态码 {proxy_status}，耗时 {total_latency}s")
            item["check_layer"] = "代理访问"
            item["raw_status_code"] = proxy_status
            return (item, total_latency)
        item["raw_status_code"] = proxy_status
        logging.warning(f"[代理失败] {link} → 状态码 {proxy_status}，进入API1")

    # 5. 加入API1队列
    item["current_latency"] = total_latency
    api1_queue.put(item)
    return (item, total_latency)


def handle_api1():
    """API1检测（直接/代理失败后执行）"""
    results = []
    with requests.Session() as session:
        while not api1_queue.empty():
            item = api1_queue.get()
            link = item["link"]
            total_latency = item["current_latency"]

            api_url = f"https://v.api.aa1.cn/api/httpcode/?url={link}"
            resp, api_latency, status = request_url(
                session, api_url, RAW_HEADERS, "API1检测", 30
            )
            total_latency = round(total_latency + api_latency, 2)

            if status == 200 and resp:
                try:
                    res_json = resp.json()
                    target_status = int(res_json.get("httpcode", -1))
                    if is_success_status_code(target_status):
                        logging.info(f"[API1成功] {link} → 目标状态码 {target_status}，耗时 {total_latency}s")
                        item["check_layer"] = "API1检测"
                        item["raw_status_code"] = target_status
                        results.append((item, total_latency))
                        continue
                    item["raw_status_code"] = target_status
                    logging.warning(f"[API1失败] {link} → 目标状态码 {target_status}，进入API2")
                except Exception as e:
                    logging.error(f"[API1解析失败] {link} → {str(e)}，进入API2")
            else:
                logging.warning(f"[API1请求失败] {link} → 状态码 {status}，进入API2")

            item["current_latency"] = total_latency
            api2_queue.put(item)
            results.append((item, total_latency))
    return results


def handle_api2():
    """API2检测（API1失败后执行）"""
    results = []
    with requests.Session() as session:
        while not api2_queue.empty():
            item = api2_queue.get()
            link = item["link"]
            total_latency = item["current_latency"]

            api_url = f"https://v2.xxapi.cn/api/status?url={link}"
            resp, api_latency, status = request_url(
                session, api_url, RAW_HEADERS, "API2检测", 30
            )
            total_latency = round(total_latency + api_latency, 2)

            if status == 200 and resp:
                try:
                    res_json = resp.json()
                    target_status = int(res_json.get("data", -1))
                    if is_success_status_code(target_status):
                        logging.info(f"[API2成功] {link} → 目标状态码 {target_status}，耗时 {total_latency}s")
                        item["check_layer"] = "API2检测"
                        item["raw_status_code"] = target_status
                        results.append((item, total_latency))
                        continue
                    item["raw_status_code"] = target_status
                    logging.warning(f"[API2失败] {link} → 目标状态码 {target_status}，检测结束")
                except Exception as e:
                    logging.error(f"[API2解析失败] {link} → {str(e)}，检测结束")
            else:
                logging.warning(f"[API2请求失败] {link} → 状态码 {status}，检测结束")

            results.append((item, total_latency))
    return results


# -------------------------- 主函数（程序入口） --------------------------
def main():
    try:
        # 1. 加载数据源
        link_list = fetch_origin_data(SOURCE_URL)
        if not link_list:
            logging.error("数据源为空，退出程序")
            return
        logging.info(f"共加载 {len(link_list)} 个链接待检测")

        # 2. 加载历史结果（用于累计失败次数）
        previous_results = load_previous_results()

        # 3. 执行直接/代理检测（多线程）
        with requests.Session() as session:
            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                initial_results = list(executor.map(
                    lambda x: check_direct_and_proxy(x, session), link_list
                ))

        # 4. 执行API1/API2检测
        api1_results = handle_api1()
        api2_results = handle_api2()

        # 5. 合并所有结果（API结果覆盖初始结果）
        result_map = {item["link"]: (item, latency) for item, latency in initial_results}
        for item, latency in api1_results + api2_results:
            if item["check_layer"] in ["API1检测", "API2检测"]:
                result_map[item["link"]] = (item, latency)
        final_results = list(result_map.values())

        # 6. 整理最终输出格式
        current_link_set = {item["link"] for item in link_list}
        link_status = []
        for item, latency in final_results:
            link = item["link"]
            if not link or link not in current_link_set:
                continue

            # 基础信息提取
            name = item.get("name", "未知名称")
            in_whitelist = item["is_whitelist"]
            ssl_ok = item["ssl_ok"]
            ssl_msg = item["ssl_message"]
            check_layer = item["check_layer"]
            status_code = item["raw_status_code"]
            final_latency = round(latency, 2)

            # 可访问性判定（核心逻辑）
            if in_whitelist:
                # 白名单链接：始终可访问
                is_accessible = True
                fail_count = 0
            else:
                parsed_url = urlparse(link)
                if parsed_url.scheme == "https" and not ssl_ok:
                    # HTTPS+SSL无效：强制不可访问（忽略后续访问结果）
                    is_accessible = False
                else:
                    # 其他情况：根据检测结果判定
                    is_accessible = check_layer != "未通过任何检测"
                
                # 累计失败次数
                prev_entry = next(
                    (x for x in previous_results.get("link_status", []) if x.get("link") == link),
                    {}
                )
                prev_fail_count = prev_entry.get("fail_count", 0)
                fail_count = prev_fail_count + 1 if not is_accessible else 0

            # 组装结果
            link_status.append({
                "name": name,
                "link": link,
                "latency": final_latency,
                "fail_count": fail_count,
                "check_layer": check_layer,
                "raw_status_code": status_code,
                "ssl_ok": ssl_ok,
                "ssl_message": ssl_msg,
                "is_whitelist": in_whitelist,
                "is_accessible": is_accessible
            })

        # 7. 统计并保存结果
        total = len(link_status)
        accessible_count = sum(1 for x in link_status if x["is_accessible"])
        output_data = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total_count": total,
            "accessible_count": accessible_count,
            "inaccessible_count": total - accessible_count,
            "link_status": link_status
        }
        save_results(output_data)
        logging.info(
            f"检测完成 → 共 {total} 个链接，可访问 {accessible_count} 个，"
            f"不可访问 {total - accessible_count} 个（结果已保存至 {RESULT_FILE}）"
        )

    except Exception as e:
        logging.exception(f"程序运行出错：{str(e)}")


if __name__ == "__main__":
    main()
