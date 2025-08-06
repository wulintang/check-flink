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
from urllib.parse import urlparse, urlunparse
import concurrent.futures

# 设置日志
logging.basicConfig(
    level=logging.INFO,
    format="😎 %(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)

warnings.filterwarnings("ignore", message="Unverified HTTPS request is being made.*")

# 更丰富的User-Agent池（覆盖更多设备和浏览器）
USER_AGENTS = [
    # 桌面浏览器
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/115.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    # 移动设备
    "Mozilla/5.0 (Linux; Android 13; SM-G998B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/112.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPad; CPU OS 16_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Mobile/15E148 Safari/604.1",
    # 冷门浏览器
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Edge/125.0.2535.85",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:109.0) Gecko/20100101 Firefox/115.0"
]

# 模拟真实浏览器的请求头（动态生成，更贴近真实）
def get_browser_headers(url):
    parsed_url = urlparse(url)
    referer = urlunparse((parsed_url.scheme, parsed_url.netloc, '', '', '', ''))
    
    # 基础 headers（所有请求都会包含）
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": random.choice([
            "zh-CN,zh;q=0.8,zh-TW;q=0.7,zh-HK;q=0.5",
            "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
            "zh-CN,zh;q=0.9"
        ]),
        "Connection": "keep-alive",
        "Referer": referer if random.random() > 0.2 else "",  # 20%概率不带referer
        "Cache-Control": random.choice(["max-age=0", "no-cache", "max-age=300"]),
        "Pragma": "no-cache" if random.random() > 0.5 else ""
    }
    
    # 随机添加可选头部（模拟不同浏览器行为）
    if random.random() > 0.6:
        headers["Accept-Encoding"] = "gzip, deflate, br"
    if random.random() > 0.7:
        headers["Upgrade-Insecure-Requests"] = "1"
    if random.random() > 0.8:
        headers["Sec-Fetch-Dest"] = "document"
        headers["Sec-Fetch-Mode"] = "navigate"
        headers["Sec-Fetch-Site"] = random.choice(["same-origin", "cross-site", "none"])
    if random.random() > 0.9:
        headers["DNT"] = "1"  # 不跟踪请求（部分用户会开启）
    
    return headers

# 可选：代理池（如果需要真实多IP，可启用；否则返回空）
def load_proxies(use_proxy=False):
    """加载代理IP池（可选），不使用代理时返回空列表"""
    if not use_proxy:
        return []
        
    proxies = []
    if os.path.exists('proxies.txt'):
        with open('proxies.txt', 'r') as f:
            for line in f:
                ip = line.strip()
                if not ip:
                    continue
                # 为IP添加协议（根据IP类型自动处理）
                if ':' in ip:  # IPv6
                    proxies.append({
                        'http': f'http://[{ip}]',
                        'https': f'https://[{ip}]'
                    })
                else:  # IPv4
                    proxies.append({
                        'http': f'http://{ip}',
                        'https': f'https://{ip}'
                    })
    return proxies

# 配置参数（默认不使用代理，模拟单IP但多行为特征）
USE_PROXY = os.getenv("USE_PROXY", "False").lower() == "true"  # 可通过环境变量控制
PROXY_POOL = load_proxies(USE_PROXY)
if PROXY_POOL:
    logging.info(f"已加载代理池，共 {len(PROXY_POOL)} 个代理（多IP模式）")
else:
    logging.info("未使用代理，模拟单IP的多行为特征（更贴近真实用户）")

SOURCE_URL = os.getenv("SOURCE_URL", "./link.csv")
RESULT_FILE = "./result.json"
api_request_queue = Queue()

# 模拟真实用户的访问间隔（更自然的分布）
def human_delay():
    """生成符合人类行为的随机延迟（避免机械规律）"""
    # 短延迟（浏览同一页面内内容）
    if random.random() > 0.3:
        return random.uniform(0.8, 3.5)
    # 长延迟（浏览后思考或切换页面）
    else:
        return random.uniform(4.0, 10.0)

def get_random_proxy():
    """随机获取代理（仅在启用时生效）"""
    if PROXY_POOL and random.random() > 0.1:  # 10%概率不使用代理（模拟真实用户偶尔直连）
        return random.choice(PROXY_POOL)
    return None

def request_url(session, url, desc="", **kwargs):
    """模拟真实用户的请求行为（核心函数）"""
    try:
        # 人类浏览延迟
        delay = human_delay()
        time.sleep(delay)
        
        # 动态生成请求头
        headers = get_browser_headers(url)
        
        # 随机超时（模拟网络波动）
        timeout = random.uniform(8, 15)
        
        # 随机代理（可选）
        proxies = get_random_proxy()
        
        start_time = time.time()
        logging.info(
            f"[{desc}] 准备访问: {url} "
            f"(延迟: {delay:.2f}s, "
            f"代理: {proxies.get('https') if proxies else '当前IP'})"
        )
        
        # 模拟真实浏览器的请求过程
        response = session.get(
            url,
            headers=headers,
            timeout=timeout,
            proxies=proxies,
            allow_redirects=True,
            stream=True
        )
        
        # 模拟浏览器逐步接收数据（大文件额外延迟）
        content_length = response.headers.get('Content-Length')
        if content_length and int(content_length) > 1024*1024*5:  # 大于5MB的内容
            time.sleep(random.uniform(1.5, 3.5))
        
        # 读取内容（模拟浏览器渲染）
        response.content
        
        latency = round(time.time() - start_time, 2)
        logging.info(
            f"[{desc}] 访问完成: {url} "
            f"状态码: {response.status_code}, "
            f"延迟: {latency}s, "
            f"大小: {len(response.content)//1024}KB"
        )
        return response, latency
    
    except requests.RequestException as e:
        logging.warning(f"[{desc}] 请求失败: {url}，错误: {str(e)[:100]}")
        return None, -1

# 以下函数（load_previous_results、save_results等）保持不变
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
                response, _ = request_url(session, origin_path, desc="数据源获取")
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
    if not is_url(link):
        logging.warning(f"无效链接格式: {link}")
        return item, -1

    # 模拟用户可能的访问方式（直接访问为主）
    access_methods = [("直接访问", link)]
    
    # 依次尝试访问
    for method, url in access_methods:
        response, latency = request_url(session, url, desc=method)
        
        if response:
            # 处理常见状态码
            if response.status_code in [200, 301, 302, 307, 308]:
                logging.info(f"[{method}] 成功访问: {link} (最终URL: {response.url})")
                return item, latency
            # 处理需要重试的情况（模拟用户刷新）
            elif response.status_code in [429, 503, 504]:
                logging.warning(f"[{method}] 需要重试: {link}，状态码: {response.status_code}")
                time.sleep(random.uniform(3, 7))  # 更长的重试间隔
                response, latency = request_url(session, url, desc=f"{method}(重试)")
                if response and response.status_code in [200, 301, 302]:
                    return item, latency
    
    # 加入API检查队列
    api_request_queue.put(item)
    return item, -1

def handle_api_requests():
    results = []
    with requests.Session() as session:
        logging.info(f"开始处理API检查队列，队列大小: {api_request_queue.qsize()}")
        while not api_request_queue.empty():
            time.sleep(human_delay())  # 人类操作间隔
            item = api_request_queue.get()
            link = item['link']
            api_url = f"https://v2.xxapi.cn/api/status?url={link}"
            
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
        # 模拟浏览器启动时间（更真实）
        start_delay = random.uniform(1.5, 3.0)
        logging.info(f"模拟浏览器启动，延迟 {start_delay:.2f}s")
        time.sleep(start_delay)
        
        link_list = fetch_origin_data(SOURCE_URL)
        if not link_list:
            logging.error("数据源为空或解析失败")
            return

        previous_results = load_previous_results()
        logging.info(f"加载历史结果: {len(previous_results.get('link_status', []))} 条记录")

        # 限制并发（模拟真实用户不会同时打开太多页面）
        max_workers = random.randint(2, 4)  # 随机2-4个并发
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            def process_with_session(item):
                with requests.Session() as session:
                    # 模拟浏览器Cookie（更真实的会话）
                    session.cookies.update({
                        "session_id": f"user_{random.randint(10000, 99999)}",
                        "visited": str(datetime.now().timestamp()),
                        "preferences": json.dumps({"theme": "light", "lang": "zh-CN"})
                    })
                    return check_link(item, session)
            
            results = list(executor.map(process_with_session, link_list))

        # 处理API检查结果
        api_results = handle_api_requests()
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
    
