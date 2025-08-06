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

# 模拟真实浏览器的User-Agent池
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/115.0",
    "Mozilla/5.0 (Linux; Android 13; SM-G998B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/112.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Mobile/15E148 Safari/604.1"
]

# 模拟真实浏览器的完整请求头（根据不同UA动态调整）
def get_browser_headers(url):
    parsed_url = urlparse(url)
    referer = urlunparse((parsed_url.scheme, parsed_url.netloc, '', '', '', ''))
    
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.8,zh-TW;q=0.7,zh-HK;q=0.5,en-US;q=0.3,en;q=0.2",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Referer": referer,
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
        "Cache-Control": f"max-age=0, no-cache",
        "Pragma": "no-cache",
        "DNT": "1"  # 不跟踪请求
    }
    
    # 随机添加一些浏览器特定头部
    if random.random() > 0.3:
        headers["TE"] = "trailers"
    if random.random() > 0.5:
        headers["Origin"] = referer
    
    return headers

# 代理池（可以从文件加载或API获取）
def load_proxies():
    """加载代理IP池，格式: [{'http': 'http://ip:port'}, {'https': 'https://ip:port'}, ...]"""
    proxies = []
    # 尝试从环境变量加载代理列表
    proxy_list = os.getenv("PROXY_LIST")
    if proxy_list:
        for proxy in proxy_list.split(';'):
            if proxy.startswith('http://'):
                proxies.append({'http': proxy, 'https': proxy})
            elif proxy.startswith('https://'):
                proxies.append({'https': proxy})
    
    # 尝试从文件加载代理
    if not proxies and os.path.exists('proxies.txt'):
        with open('proxies.txt', 'r') as f:
            for line in f:
                line = line.strip()
                if line and (line.startswith('http://') or line.startswith('https://')):
                    if line.startswith('http://'):
                        proxies.append({'http': line, 'https': line})
                    else:
                        proxies.append({'https': line})
    
    return proxies

# 全局代理池
PROXY_POOL = load_proxies()
if PROXY_POOL:
    logging.info(f"已加载代理池，共 {len(PROXY_POOL)} 个代理")
else:
    logging.info("未加载到代理池，将使用本地IP")

# 配置参数
SOURCE_URL = os.getenv("SOURCE_URL", "./link.csv")  # 默认本地文件
RESULT_FILE = "./result.json"
api_request_queue = Queue()

def get_random_proxy():
    """随机获取一个代理，模拟不同IP"""
    if PROXY_POOL and random.random() > 0.2:  # 20%概率不使用代理，更接近真实用户行为
        return random.choice(PROXY_POOL)
    return None

def request_url(session, url, desc="", timeout=None, **kwargs):
    """模拟真实浏览器的请求函数，包含随机延迟和行为特征"""
    try:
        # 模拟人类浏览的随机延迟（0.5-3秒）
        delay = random.uniform(0.5, 3.0)
        time.sleep(delay)
        
        # 动态生成请求头
        headers = get_browser_headers(url)
        
        # 随机超时设置（10-20秒）
        timeout = timeout or random.uniform(10, 20)
        
        # 获取随机代理
        proxies = get_random_proxy()
        
        start_time = time.time()
        logging.info(f"[{desc}] 准备访问: {url} (延迟: {delay:.2f}s, 代理: {proxies.get('https') if proxies else '本地IP'})")
        
        # 模拟真实浏览器的请求行为
        response = session.get(
            url,
            headers=headers,
            timeout=timeout,
            proxies=proxies,
            allow_redirects=True,  # 允许重定向，模拟浏览器行为
            stream=True,  # 流式下载，模拟浏览器逐步接收数据
           ** kwargs
        )
        
        # 模拟浏览器接收数据的过程
        content_length = response.headers.get('Content-Length')
        if content_length and int(content_length) > 1024 * 1024:  # 大于1MB的内容模拟延迟
            time.sleep(random.uniform(0.5, 2.0))
        
        # 读取响应内容（模拟浏览器处理）
        response.content  # 触发内容下载
        
        latency = round(time.time() - start_time, 2)
        logging.info(f"[{desc}] 访问完成: {url}，状态码: {response.status_code}, 延迟: {latency}s, 大小: {len(response.content)//1024}KB")
        return response, latency
    
    except requests.RequestException as e:
        logging.warning(f"[{desc}] 请求失败: {url}，错误: {str(e)[:100]}")
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
                # 模拟浏览器获取数据源
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

    # 模拟真实用户可能尝试的访问方式
    access_methods = [("直接访问", link)]
    
    # 如果有代理模板，添加代理访问方式
    proxy_url_template = os.getenv("PROXY_URL_TEMPLATE")
    if proxy_url_template:
        access_methods.append(("代理访问", proxy_url_template.format(link)))
    
    # 依次尝试不同访问方式
    for method, url in access_methods:
        response, latency = request_url(session, url, desc=method)
        
        # 检查响应是否有效（模拟浏览器对不同状态码的处理）
        if response:
            # 处理常见的成功状态码
            if response.status_code in [200, 301, 302, 307, 308]:
                logging.info(f"[{method}] 成功访问: {link} (最终URL: {response.url})")
                return item, latency
            # 处理需要重试的状态码
            elif response.status_code in [429, 503, 504]:
                logging.warning(f"[{method}] 需要重试: {link}，状态码: {response.status_code}")
                # 模拟浏览器重试
                time.sleep(random.uniform(2, 5))
                response, latency = request_url(session, url, desc=f"{method}(重试)")
                if response and response.status_code in [200, 301, 302]:
                    return item, latency
    
    # 所有方式失败，加入API检查队列
    logging.info(f"所有方式均失败，将 {link} 加入API检查队列")
    api_request_queue.put(item)
    return item, -1

def handle_api_requests():
    results = []
    # 为API检查创建新的Session，模拟不同浏览器实例
    with requests.Session() as session:
        logging.info(f"开始处理API检查队列，队列大小: {api_request_queue.qsize()}")
        while not api_request_queue.empty():
            # 模拟人类操作间隔
            time.sleep(random.uniform(1, 3))
            item = api_request_queue.get()
            link = item['link']
            api_url = f"https://v2.xxapi.cn/api/status?url={link}"
            
            response, latency = request_url(session, api_url, desc="API检查", timeout=30)
            if response:
                try:
                    res_json = response.json()
                    if int(res_json.get("code", -1)) == 200 and int(res_json.get("data", -1)) == 200:
                        logging.info(f"[API] 确认可访问: {link}")
                        results.append((item, latency))
                    else:
                        logging.warning(f"[API] 确认不可访问: {link}，响应: {res_json}")
                        results.append((item, -1))
                except Exception as e:
                    logging.error(f"[API] 解析失败: {link}，错误: {e}")
                    results.append((item, -1))
            else:
                logging.warning(f"[API] 请求失败: {link}")
                results.append((item, -1))
            api_request_queue.task_done()
    logging.info("API检查队列处理完成")
    return results

def main():
    try:
        # 模拟浏览器启动时间
        start_delay = random.uniform(1, 2)
        logging.info(f"模拟浏览器启动，延迟 {start_delay:.2f}s")
        time.sleep(start_delay)
        
        link_list = fetch_origin_data(SOURCE_URL)
        if not link_list:
            logging.error("数据源为空或解析失败")
            return

        previous_results = load_previous_results()
        logging.info(f"加载历史结果: {len(previous_results.get('link_status', []))} 条记录")

        # 创建多个Session模拟不同浏览器实例
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:  # 减少并发，更接近真实用户行为
            # 为每个线程创建独立的Session
            def process_with_session(item):
                with requests.Session() as session:
                    # 模拟浏览器初始化（设置Cookie等）
                    session.cookies.update({
                        "visited": "true",
                        "timestamp": str(int(time.time()))
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
    
