#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import time
import re
import json
import math
import requests
import threading
import queue
import logging
import urllib3
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, jsonify, send_from_directory

# 检查是否在 Render 环境
IS_RENDER = os.environ.get('RENDER', False)

# 简化日志配置
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# 禁用不必要的警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=os.path.join(BASE_DIR, 'static'), static_url_path='/')

# 延迟导入可能耗时的模块
try:
    from binance.client import Client
    from flask_cors import CORS
    CORS(app)
    BINANCE_AVAILABLE = True
except ImportError as e:
    logger.warning(f"❌ 部分依赖不可用: {e}")
    BINANCE_AVAILABLE = False

# Binance API 配置
API_KEY = os.environ.get('BINANCE_API_KEY', '')
API_SECRET = os.environ.get('BINANCE_API_SECRET', '')
client = None

# 数据缓存
data_cache = {
    "last_updated": "从未更新",
    "daily_rising": [],
    "short_term_active": [],
    "all_cycle_rising": [],
    "analysis_time": 0,
    "next_analysis_time": "计算中..."
}

current_data_cache = data_cache.copy()
oi_data_cache = {}
resistance_cache = {}
RESISTANCE_CACHE_EXPIRATION = 24 * 3600
OI_CACHE_EXPIRATION = 5 * 60

# 使用队列进行线程间通信
analysis_queue = queue.Queue()
executor = ThreadPoolExecutor(max_workers=3)  # 减少工作线程数

# 周期配置
PERIOD_MINUTES = {
    '5m': 5, '15m': 15, '30m': 30, '1h': 60, '2h': 120,
    '4h': 240, '6h': 360, '12h': 720, '1d': 1440
}

VALID_PERIODS = ['5m', '15m', '30m', '1h', '2h', '4h', '6h', '12h', '1d']
RESISTANCE_INTERVALS = ['1m', '15m', '1d']

def init_client():
    """初始化 Binance 客户端"""
    global client
    
    if not BINANCE_AVAILABLE:
        logger.error("❌ Binance 客户端不可用，缺少依赖")
        return False
        
    if not API_KEY or not API_SECRET:
        logger.error("❌ Binance API密钥未设置")
        return False
    
    max_retries = 3
    retry_delay = 5
    
    for attempt in range(max_retries):
        try:
            logger.info(f"🔧 尝试初始化Binance客户端 (第{attempt+1}次)...")
            client = Client(
                api_key=API_KEY, 
                api_secret=API_SECRET,
                requests_params={'timeout': 20}
            )
            
            server_time = client.get_server_time()
            logger.info(f"✅ Binance客户端初始化成功")
            return True
        except Exception as e:
            logger.error(f"❌ 初始化Binance客户端失败: {str(e)}")
            if attempt < max_retries - 1:
                logger.info(f"🔄 {retry_delay}秒后重试...")
                time.sleep(retry_delay)
    
    logger.critical("🔥 无法初始化Binance客户端")
    return False

def get_next_update_time(period):
    """计算下次更新时间"""
    tz_shanghai = timezone(timedelta(hours=8))
    now = datetime.now(tz_shanghai)
    minutes = PERIOD_MINUTES.get(period, 5)
    
    if period.endswith('m'):
        period_minutes = int(period[:-1])
        current_minute = now.minute
        current_period_minute = (current_minute // period_minutes) * period_minutes
        
        base_time = now.replace(minute=current_period_minute, second=0, microsecond=0)
        next_update = base_time + timedelta(minutes=period_minutes, seconds=60)
        
        if next_update < now:
            next_update += timedelta(minutes=period_minutes)
    elif period.endswith('h'):
        period_hours = int(period[:-1])
        current_hour = now.hour
        current_period_hour = (current_hour // period_hours) * period_hours
        
        base_time = now.replace(hour=current_period_hour, minute=0, second=0, microsecond=0)
        next_update = base_time + timedelta(hours=period_hours, seconds=60)
        
        if next_update < now:
            next_update += timedelta(hours=period_hours)
    else:
        base_time = now.replace(hour=0, minute=0, second=0, microsecond=0)
        next_update = base_time + timedelta(days=1, seconds=60)

    return next_update

def get_open_interest(symbol, period, use_cache=True):
    """获取持仓量数据"""
    try:
        if not re.match(r"^[A-Z0-9]{1,10}USDT$", symbol):
            return {'series': [], 'timestamps': []}

        if period not in VALID_PERIODS:
            return {'series': [], 'timestamps': []}
        
        current_time = datetime.now(timezone.utc)
        cache_key = f"{symbol}_{period}"
        
        if use_cache and cache_key in oi_data_cache:
            cached_data = oi_data_cache[cache_key]
            if 'expiration' in cached_data and cached_data['expiration'] > current_time:
                return cached_data['data']

        logger.info(f"📡 请求持仓量数据: {symbol} {period}")
        url = "https://fapi.binance.com/futures/data/openInterestHist"
        params = {'symbol': symbol, 'period': period, 'limit': 30}

        response = requests.get(url, params=params, timeout=10)
        
        if response.status_code != 200:
            return {'series': [], 'timestamps': []}

        data = response.json()
        
        if not isinstance(data, list) or len(data) == 0:
            return {'series': [], 'timestamps': []}
            
        data.sort(key=lambda x: x['timestamp'])
        oi_series = [float(item['sumOpenInterest']) for item in data]
        timestamps = [item['timestamp'] for item in data]

        if len(oi_series) < 30:
            return {'series': [], 'timestamps': []}
            
        oi_data = {
            'series': oi_series, 
            'timestamps': timestamps
        }
        
        expiration = current_time + timedelta(seconds=OI_CACHE_EXPIRATION)
        oi_data_cache[cache_key] = {
            'data': oi_data,
            'expiration': expiration
        }

        return oi_data
    except Exception as e:
        logger.error(f"❌ 获取{symbol}的{period}持仓量失败: {str(e)}")
        return {'series': [], 'timestamps': []}

def is_latest_highest(oi_series):
    """检查是否为近期最高点"""
    if not oi_series or len(oi_series) < 30:
        return False

    latest_value = oi_series[-1]
    prev_data = oi_series[-30:-1]
    
    return latest_value > max(prev_data) if prev_data else False

def calculate_support_resistance_levels(symbol, interval, klines):
    """计算支撑位和阻力位"""
    try:
        if not klines or len(klines) < 20:
            return {'resistance': [], 'support': []}
        
        # 提取收盘价
        closes = [float(k[4]) for k in klines]
        
        # 计算价格水平及其被测试次数
        price_levels = {}
        tolerance = 0.001
        
        for i in range(1, len(closes)-1):
            current_close = closes[i]
            
            # 寻找局部高点和低点
            is_local_high = closes[i] > closes[i-1] and closes[i] > closes[i+1]
            is_local_low = closes[i] < closes[i-1] and closes[i] < closes[i+1]
            
            # 四舍五入到合适的精度
            if current_close > 100:
                rounded_price = round(current_close, 1)
            elif current_close > 10:
                rounded_price = round(current_close, 2)
            elif current_close > 1:
                rounded_price = round(current_close, 3)
            else:
                rounded_price = round(current_close, 4)
            
            # 检查是否接近现有价格水平
            found_existing = False
            for existing_price in price_levels.keys():
                if abs(existing_price - rounded_price) / existing_price <= tolerance:
                    price_levels[existing_price]['count'] += 1
                    if is_local_high:
                        price_levels[existing_price]['resistance_tests'] += 1
                    if is_local_low:
                        price_levels[existing_price]['support_tests'] += 1
                    found_existing = True
                    break
            
            if not found_existing:
                price_levels[rounded_price] = {
                    'count': 1,
                    'resistance_tests': 1 if is_local_high else 0,
                    'support_tests': 1 if is_local_low else 0
                }
        
        # 获取当前价格
        current_price = closes[-1] if closes else 0
        
        # 分离阻力位和支撑位
        resistance_levels = []
        support_levels = []
        
        for price, data in price_levels.items():
            if data['resistance_tests'] > 0 or data['support_tests'] > 0:
                total_tests = data['resistance_tests'] + data['support_tests']
                strength = min(1.0, total_tests / 10.0)
                
                level_data = {
                    'price': price,
                    'strength': round(strength, 2),
                    'test_count': total_tests,
                    'resistance_tests': data['resistance_tests'],
                    'support_tests': data['support_tests'],
                    'distance_percent': round(((price - current_price) / current_price * 100), 2) if current_price > 0 else 0
                }
                
                if price > current_price and data['resistance_tests'] > 0:
                    resistance_levels.append(level_data)
                elif price < current_price and data['support_tests'] > 0:
                    support_levels.append(level_data)
        
        # 按被测试次数排序，只保留前3个
        resistance_levels.sort(key=lambda x: x['test_count'], reverse=True)
        support_levels.sort(key=lambda x: x['test_count'], reverse=True)
        
        return {
            'resistance': resistance_levels[:3],
            'support': support_levels[:3]
        }
        
    except Exception as e:
        logger.error(f"计算支撑阻力位失败 {symbol} {interval}: {str(e)}")
        return {'resistance': [], 'support': []}

def calculate_resistance_levels(symbol):
    """计算阻力位"""
    try:
        logger.info(f"📊 计算阻力位: {symbol}")
        now = time.time()
        
        cache_key = f"{symbol}_resistance"
        if cache_key in resistance_cache:
            cache_data = resistance_cache[cache_key]
            if cache_data['expiration'] > now:
                return cache_data['levels']
        
        if client is None and not init_client():
            return {'levels': {}, 'current_price': 0}
        
        try:
            ticker = client.futures_symbol_ticker(symbol=symbol)
            current_price = float(ticker['price'])
        except Exception:
            current_price = None
        
        interval_levels = {}
        
        for interval in RESISTANCE_INTERVALS:
            try:
                klines = client.futures_klines(symbol=symbol, interval=interval, limit=100)
                
                if not klines or len(klines) < 20:
                    continue

                levels = calculate_support_resistance_levels(symbol, interval, klines)
                interval_levels[interval] = levels
                
            except Exception as e:
                logger.error(f"计算{symbol}在{interval}的阻力位失败: {str(e)}")

        levels = {
            'levels': interval_levels,
            'current_price': current_price or 0
        }
        
        resistance_cache[cache_key] = {
            'levels': levels,
            'expiration': now + RESISTANCE_CACHE_EXPIRATION
        }
        return levels
    except Exception as e:
        logger.error(f"计算{symbol}的阻力位失败: {str(e)}")
        return {'levels': {}, 'current_price': 0}

def analyze_symbol(symbol):
    """分析单个币种"""
    try:
        symbol_result = {
            'symbol': symbol,
            'daily_rising': None,
            'short_term_active': None,
            'all_cycle_rising': None,
            'period_status': {p: False for p in VALID_PERIODS},
            'period_count': 0
        }

        daily_oi = get_open_interest(symbol, '1d')
        daily_series = daily_oi.get('series', [])
        
        if len(daily_series) >= 30:
            daily_status = is_latest_highest(daily_series)
            symbol_result['period_status']['1d'] = daily_status
            
            if daily_status:
                daily_change = ((daily_series[-1] - daily_series[-30]) / daily_series[-30]) * 100
                
                daily_rising_item = {
                    'symbol': symbol,
                    'oi': daily_series[-1],
                    'change': round(daily_change, 2),
                    'period_status': symbol_result['period_status'].copy()
                }
                symbol_result['daily_rising'] = daily_rising_item
                symbol_result['period_count'] = 1

                all_intervals_up = True
                for period in VALID_PERIODS:
                    if period == '1d':
                        continue
                        
                    oi_data = get_open_interest(symbol, period)
                    oi_series = oi_data.get('series', [])
                    
                    status = len(oi_series) >= 30 and is_latest_highest(oi_series)
                    symbol_result['period_status'][period] = status
                    
                    if status:
                        symbol_result['period_count'] += 1
                    else:
                        all_intervals_up = False

                if all_intervals_up:
                    symbol_result['all_cycle_rising'] = {
                        'symbol': symbol,
                        'oi': daily_series[-1],
                        'change': round(daily_change, 2),
                        'period_count': symbol_result['period_count'],
                        'period_status': symbol_result['period_status'].copy()
                    }
                
                if symbol_result['daily_rising']:
                    symbol_result['daily_rising']['period_status'] = symbol_result['period_status'].copy()
                    symbol_result['daily_rising']['period_count'] = symbol_result['period_count']

        min5_oi = get_open_interest(symbol, '5m')
        min5_series = min5_oi.get('series', [])
        
        if len(min5_series) >= 30 and len(daily_series) >= 30:
            min5_max = max(min5_series[-30:])
            daily_avg = sum(daily_series[-30:]) / 30
            
            if min5_max > 0 and daily_avg > 0:
                ratio = min5_max / daily_avg
                
                if ratio > 1.5:
                    symbol_result['short_term_active'] = {
                        'symbol': symbol,
                        'oi': min5_series[-1],
                        'ratio': round(ratio, 2)
                    }

        return symbol_result
    except Exception as e:
        logger.error(f"❌ 处理{symbol}时出错: {str(e)}")
        return {
            'symbol': symbol,
            'period_status': {p: False for p in VALID_PERIODS},
            'period_count': 0
        }

def get_high_volume_symbols():
    """获取高交易量币种"""
    if client is None and not init_client():
        return []

    try:
        tickers = client.futures_ticker()
        filtered = [
            t for t in tickers if float(t.get('quoteVolume', 0)) > 10000000
            and t.get('symbol', '').endswith('USDT')
        ]
        return [t['symbol'] for t in filtered[:20]]  # 限制币种数量
    except Exception as e:
        logger.error(f"❌ 获取高交易量币种失败: {str(e)}")
        return []

def analyze_trends():
    """分析趋势"""
    start_time = time.time()
    logger.info("🔍 开始分析币种趋势...")
    symbols = get_high_volume_symbols()
    
    if not symbols:
        return data_cache

    daily_rising = []
    short_term_active = []
    all_cycle_rising = []

    futures = [executor.submit(analyze_symbol, symbol) for symbol in symbols]
    
    for future in as_completed(futures):
        try:
            result = future.result()
            if result.get('daily_rising'):
                daily_rising.append(result['daily_rising'])
            if result.get('short_term_active'):
                short_term_active.append(result['short_term_active'])
            if result.get('all_cycle_rising'):
                all_cycle_rising.append(result['all_cycle_rising'])
        except Exception as e:
            logger.error(f"❌ 处理币种时出错: {str(e)}")

    daily_rising.sort(key=lambda x: x.get('period_count', 0), reverse=True)
    short_term_active.sort(key=lambda x: x.get('ratio', 0), reverse=True)
    all_cycle_rising.sort(key=lambda x: x.get('period_count', 0), reverse=True)

    analysis_time = time.time() - start_time
    logger.info(f"📊 分析完成: 日线上涨 {len(daily_rising)}个, 短期活跃 {len(short_term_active)}个, 全部周期上涨 {len(all_cycle_rising)}个")

    tz_shanghai = timezone(timedelta(hours=8))
    return {
        'daily_rising': daily_rising,
        'short_term_active': short_term_active,
        'all_cycle_rising': all_cycle_rising,
        'analysis_time': analysis_time,
        'last_updated': datetime.now(tz_shanghai).strftime("%Y-%m-%d %H:%M:%S"),
        'next_analysis_time': get_next_update_time('5m').strftime("%Y-%m-%d %H:%M:%S")
    }

def analysis_worker():
    """分析工作线程"""
    global data_cache, current_data_cache
    logger.info("🔧 数据分析线程启动")

    while True:
        try:
            task = analysis_queue.get()
            if task == "STOP":
                logger.info("🛑 收到停止信号，结束分析线程")
                analysis_queue.task_done()
                break

            analysis_start = datetime.now(timezone.utc)
            logger.info(f"⏱️ 开始更新数据...")

            backup_cache = data_cache.copy()
            current_backup = current_data_cache.copy()

            try:
                result = analyze_trends()
                
                new_data = {
                    "last_updated": result['last_updated'],
                    "daily_rising": result['daily_rising'],
                    "short_term_active": result['short_term_active'],
                    "all_cycle_rising": result['all_cycle_rising'],
                    "analysis_time": result['analysis_time'],
                    "next_analysis_time": result['next_analysis_time']
                }
                
                data_cache = new_data
                current_data_cache = new_data.copy()
                logger.info(f"✅ 数据更新成功")
            except Exception as e:
                logger.error(f"❌ 分析过程中出错: {str(e)}")
                data_cache = backup_cache
                current_data_cache = current_backup

            analysis_duration = (datetime.now(timezone.utc) - analysis_start).total_seconds()
            logger.info(f"⏱️ 分析耗时: {analysis_duration:.2f}秒")
            
            next_time = get_next_update_time('5m')
            wait_seconds = max(60, (next_time - datetime.now(timezone.utc)).total_seconds())
            logger.info(f"⏳ 下次分析将在 {wait_seconds:.1f} 秒后")
            
            time.sleep(wait_seconds)
            analysis_queue.task_done()
        except Exception as e:
            logger.error(f"❌ 分析失败: {str(e)}")
            analysis_queue.task_done()

def start_background_threads():
    """启动后台线程"""
    static_path = app.static_folder
    if not os.path.exists(static_path):
        os.makedirs(static_path)
    
    index_path = os.path.join(static_path, 'index.html')
    if not os.path.exists(index_path):
        with open(index_path, 'w') as f:
            f.write("<html><body><h1>持仓量分析服务正在启动...</h1></body></html>")
    
    global current_data_cache
    tz_shanghai = timezone(timedelta(hours=8))
    current_data_cache = {
        "last_updated": "等待首次分析",
        "daily_rising": [],
        "short_term_active": [],
        "all_cycle_rising": [],
        "analysis_time": 0,
        "next_analysis_time": get_next_update_time('5m').strftime("%Y-%m-%d %H:%M:%S")
    }
    
    # 延迟启动分析线程（避免阻塞应用启动）
    def delayed_start():
        time.sleep(10)  # 等待10秒让应用先启动
        
        # 初始化客户端
        if not init_client():
            logger.error("❌ 无法初始化客户端，部分功能可能不可用")
        
        # 启动分析线程
        worker_thread = threading.Thread(target=analysis_worker, name="AnalysisWorker")
        worker_thread.daemon = True
        worker_thread.start()
        
        # 提交初始分析任务
        time.sleep(5)
        analysis_queue.put("ANALYZE")
        logger.info("🔄 已提交初始分析任务")
    
    start_thread = threading.Thread(target=delayed_start)
    start_thread.daemon = True
    start_thread.start()
    
    logger.info("✅ 后台线程启动成功")
    return True

# API路由
@app.route('/')
def index():
    try:
        return send_from_directory(app.static_folder, 'index.html')
    except Exception as e:
        logger.error(f"❌ 处理首页请求失败: {str(e)}")
        return "服务正在启动，请稍后刷新...", 200

@app.route('/<path:filename>')
def static_files(filename):
    return send_from_directory(app.static_folder, filename)

@app.route('/api/data', methods=['GET'])
def get_data():
    global current_data_cache
    try:
        if not current_data_cache or current_data_cache.get("last_updated") == "从未更新":
            return jsonify({
                'last_updated': "数据生成中...",
                'daily_rising': [],
                'short_term_active': [],
                'all_cycle_rising': [],
                'analysis_time': 0,
                'next_analysis_time': "计算中..."
            })
        
        def validate_coins(coins):
            valid_coins = []
            for coin in coins:
                if not isinstance(coin, dict):
                    continue
                if 'symbol' not in coin:
                    coin['symbol'] = '未知币种'
                if 'oi' not in coin:
                    coin['oi'] = 0
                if 'change' not in coin:
                    coin['change'] = 0
                if 'ratio' not in coin:
                    coin['ratio'] = 0
                if 'period_count' not in coin:
                    coin['period_count'] = 0
                if 'period_status' not in coin:
                    coin['period_status'] = {}
                valid_coins.append(coin)
            return valid_coins
        
        daily_rising = validate_coins(current_data_cache.get('daily_rising', []))
        short_term_active = validate_coins(current_data_cache.get('short_term_active', []))
        all_cycle_rising = validate_coins(current_data_cache.get('all_cycle_rising', []))
        
        all_cycle_symbols = {coin['symbol'] for coin in all_cycle_rising}
        filtered_daily_rising = [coin for coin in daily_rising if coin['symbol'] not in all_cycle_symbols]
        
        data = {
            'last_updated': current_data_cache.get('last_updated', ""),
            'daily_rising': filtered_daily_rising,
            'short_term_active': short_term_active,
            'all_cycle_rising': all_cycle_rising,
            'analysis_time': current_data_cache.get('analysis_time', 0),
            'next_analysis_time': current_data_cache.get('next_analysis_time', "")
        }
        
        return jsonify(data)
    
    except Exception as e:
        logger.error(f"❌ 获取数据失败: {str(e)}")
        tz_shanghai = timezone(timedelta(hours=8))
        return jsonify({
            'last_updated': datetime.now(tz_shanghai).strftime("%Y-%m-%d %H:%M:%S"),
            'daily_rising': [],
            'short_term_active': [],
            'all_cycle_rising': [],
            'analysis_time': 0,
            'next_analysis_time': get_next_update_time('5m').strftime("%Y-%m-%d %H:%M:%S")
        })

@app.route('/api/resistance_levels/<symbol>', methods=['GET'])
def get_resistance_levels(symbol):
    try:
        if not re.match(r"^[A-Z0-9]{2,10}USDT$", symbol):
            return jsonify({'error': 'Invalid symbol format'}), 400

        if not BINANCE_AVAILABLE:
            return jsonify({'error': 'Binance client not available'}), 503

        levels = calculate_resistance_levels(symbol)
        
        return jsonify(levels)
    except Exception as e:
        logger.error(f"❌ 获取阻力位数据失败: {symbol}, {str(e)}")
        return jsonify({
            'levels': {},
            'current_price': 0,
            'error': str(e)
        }), 500

@app.route('/api/oi_chart/<symbol>/<period>', methods=['GET'])
def get_oi_chart_data(symbol, period):
    try:
        if not re.match(r"^[A-Z0-9]{2,10}USDT$", symbol):
            return jsonify({'error': 'Invalid symbol format'}), 400

        if period not in VALID_PERIODS:
            return jsonify({'error': 'Unsupported period'}), 400

        oi_data = get_open_interest(symbol, period, use_cache=True)
        return jsonify({
            'data': oi_data.get('series', []),
            'timestamps': oi_data.get('timestamps', [])
        })
    except Exception as e:
        logger.error(f"❌ 获取持仓量图表数据失败: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/health', methods=['GET'])
def health_check():
    try:
        binance_status = 'not initialized'
        if client:
            try:
                client.get_server_time()
                binance_status = 'ok'
            except:
                binance_status = 'error'
        
        tz_shanghai = timezone(timedelta(hours=8))
        return jsonify({
            'status': 'healthy',
            'binance': binance_status,
            'last_updated': current_data_cache.get('last_updated', 'N/A'),
            'next_analysis_time': current_data_cache.get('next_analysis_time', 'N/A'),
            'server_time': datetime.now(tz_shanghai).strftime("%Y-%m-%d %H:%M:%S"),
            'environment': 'render' if IS_RENDER else 'local'
        })
    except Exception as e:
        return jsonify({
            'status': 'unhealthy',
            'error': str(e)
        }), 500

@app.route('/api/status', methods=['GET'])
def status():
    """简化状态检查"""
    return jsonify({
        'status': 'running',
        'timestamp': datetime.now().isoformat()
    })

if __name__ == '__main__':
    PORT = int(os.environ.get("PORT", 10000))
    
    logger.info("=" * 50)
    logger.info(f"🚀 启动加密货币持仓量分析服务")
    logger.info(f"🌐 服务端口: {PORT}")
    logger.info(f"🏷️ 环境: {'Render' if IS_RENDER else 'Local'}")
    logger.info("=" * 50)
    
    if start_background_threads():
        logger.info("🚀 启动服务器...")
        
        # 如果在 Render 环境，使用更简单的启动方式
        if IS_RENDER:
            # 使用 Gunicorn 兼容的方式
            app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
        else:
            app.run(host='0.0.0.0', port=PORT, debug=False)
    else:
        logger.critical("🔥 无法启动服务")
