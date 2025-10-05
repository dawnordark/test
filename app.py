#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import subprocess
import traceback

# 完全移除 TA-Lib 依赖
talib = None
print("ℹ️ 使用内置技术指标计算函数，无需外部依赖")

import time
import re
import json
import math
import requests
import threading
import queue
import logging
import urllib3
import numpy as np
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, jsonify, send_from_directory
from binance.client import Client
from flask_cors import CORS

# 禁用不必要的警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 设置日志级别
LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO').upper()
root_logger = logging.getLogger()
root_logger.setLevel(getattr(logging, LOG_LEVEL))

# 创建日志处理器
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(getattr(logging, LOG_LEVEL))
file_handler = logging.FileHandler('app.log')
file_handler.setLevel(getattr(logging, LOG_LEVEL))

# 日志格式
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
console_handler.setFormatter(formatter)
file_handler.setFormatter(formatter)

# 添加处理器
root_logger.addHandler(console_handler)
root_logger.addHandler(file_handler)

logger = logging.getLogger(__name__)
logger.info(f"✅ 日志级别设置为: {LOG_LEVEL}")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=os.path.join(BASE_DIR, 'static'), static_url_path='/')
# 扩展 CORS 配置
CORS(app, resources={
    r"/api/*": {"origins": "*"},
    r"/static/*": {"origins": "*"},
    r"/*": {"origins": "*"}
})

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
executor = ThreadPoolExecutor(max_workers=10)

# 只保留有效的9个周期
PERIOD_MINUTES = {
    '5m': 5,
    '15m': 15,
    '30m': 30,
    '1h': 60,
    '2h': 120,
    '4h': 240,
    '6h': 360,
    '12h': 720,
    '1d': 1440
}

VALID_PERIODS = ['5m', '15m', '30m', '1h', '2h', '4h', '6h', '12h', '1d']
RESISTANCE_INTERVALS = ['5m', '15m', '30m', '1h', '2h', '4h', '6h', '12h', '1d']

# 指标权重配置
INDICATOR_WEIGHTS = {
    'fib_0.618': 1.5,
    'ema_200': 1.4,
    'ema_100': 1.2,
    'bb_upper': 1.1,
    'bb_lower': 1.1,
    'fib_other': 1.0,
    'ema_50': 0.9,
    'integer': 0.8,
    'volume_profile': 1.8,
    'orderbook': 2.0
}

def init_client():
    global client
    max_retries = 5
    retry_delay = 5
    
    if not API_KEY or not API_SECRET:
        logger.error("❌ Binance API密钥未设置")
        return False
    
    for attempt in range(max_retries):
        try:
            logger.info(f"🔧 尝试初始化Binance客户端 (第{attempt+1}次)...")
            client = Client(
                api_key=API_KEY, 
                api_secret=API_SECRET,
                requests_params={'timeout': 30}
            )
            
            server_time = client.get_server_time()
            logger.info(f"✅ Binance客户端初始化成功，服务器时间: {datetime.fromtimestamp(server_time['serverTime']/1000)}")
            return True
        except Exception as e:
            logger.error(f"❌ 初始化Binance客户端失败: {str(e)}")
            if attempt < max_retries - 1:
                logger.info(f"🔄 {retry_delay}秒后重试初始化客户端...")
                time.sleep(retry_delay)
    logger.critical("🔥 无法初始化Binance客户端，已达到最大重试次数")
    return False

def get_next_update_time(period):
    tz_shanghai = timezone(timedelta(hours=8))
    now = datetime.now(tz_shanghai)
    minutes = PERIOD_MINUTES.get(period, 5)
    
    if period.endswith('m'):
        period_minutes = int(period[:-1])
        current_minute = now.minute
        current_period_minute = (current_minute // period_minutes) * period_minutes
        next_update = now.replace(minute=current_period_minute, second=2, microsecond=0) + timedelta(minutes=period_minutes)
        if next_update < now:
            next_update += timedelta(minutes=period_minutes)
    elif period.endswith('h'):
        period_hours = int(period[:-1])
        current_hour = now.hour
        current_period_hour = (current_hour // period_hours) * period_hours
        next_update = now.replace(hour=current_period_hour, minute=0, second=2, microsecond=0) + timedelta(hours=period_hours)
    else:
        next_update = now.replace(hour=0, minute=0, second=2, microsecond=0) + timedelta(days=1)

    return next_update

def get_open_interest(symbol, period, use_cache=True):
    try:
        if not re.match(r"^[A-Z0-9]{1,10}USDT$", symbol):
            logger.warning(f"⚠️ 无效的币种名称: {symbol}")
            return {'series': [], 'timestamps': []}

        if period not in VALID_PERIODS:
            logger.warning(f"⚠️ 不支持的周期: {period}")
            return {'series': [], 'timestamps': []}
        
        current_time = datetime.now(timezone.utc)
        cache_key = f"{symbol}_{period}"
        
        if use_cache and cache_key in oi_data_cache:
            cached_data = oi_data_cache[cache_key]
            if 'expiration' in cached_data and cached_data['expiration'] > current_time:
                logger.debug(f"📈 使用缓存数据: {symbol} {period}")
                return cached_data['data']

        logger.info(f"📡 请求持仓量数据: symbol={symbol}, period={period}")
        url = "https://fapi.binance.com/futures/data/openInterestHist"
        params = {'symbol': symbol, 'period': period, 'limit': 30}

        response = requests.get(url, params=params, timeout=15)
        logger.debug(f"📡 响应状态: {response.status_code}")

        if response.status_code != 200:
            logger.error(f"❌ 获取{symbol}的{period}持仓量失败: HTTP {response.status_code}")
            return {'series': [], 'timestamps': []}

        data = response.json()
        logger.debug(f"📡 获取到 {len(data)} 条持仓量数据")

        if not isinstance(data, list) or len(data) == 0:
            logger.warning(f"⚠️ {symbol}的{period}持仓量数据为空")
            return {'series': [], 'timestamps': []}
            
        data.sort(key=lambda x: x['timestamp'])
        oi_series = [float(item['sumOpenInterest']) for item in data]
        timestamps = [item['timestamp'] for item in data]

        if len(oi_series) < 30:
            logger.warning(f"⚠️ {symbol}的{period}持仓量数据不足30个点")
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

        logger.info(f"📈 获取新数据: {symbol} {period} ({len(oi_series)}点)")
        return oi_data
    except Exception as e:
        logger.error(f"❌ 获取{symbol}的{period}持仓量失败: {str(e)}")
        logger.error(traceback.format_exc())
        return {'series': [], 'timestamps': []}

def is_latest_highest(oi_data):
    if not oi_data or len(oi_data) < 30:
        logger.debug("持仓量数据不足30个点")
        return False

    latest_value = oi_data[-1]
    prev_data = oi_data[-30:-1]
    
    return latest_value > max(prev_data) if prev_data else False

def add_volume_profile(symbol, interval):
    try:
        klines = client.futures_klines(symbol=symbol, interval=interval, limit=500)
        if not klines or len(klines) < 100:
            return []
        
        prices = [float(k[4]) for k in klines]
        volumes = [float(k[5]) for k in klines]
        
        price_range = max(prices) - min(prices)
        bin_size = price_range / 20
        
        volume_profile = {}
        for i in range(len(prices)):
            bin_key = round(prices[i] / bin_size) * bin_size
            volume_profile[bin_key] = volume_profile.get(bin_key, 0) + volumes[i]
        
        sorted_profile = sorted(volume_profile.items(), key=lambda x: x[1], reverse=True)
        return [item[0] for item in sorted_profile[:3]]
    
    except Exception as e:
        logger.error(f"成交量剖面分析失败: {str(e)}")
        return []

def get_orderbook_liquidity(symbol):
    try:
        orderbook = client.futures_order_book(symbol=symbol, limit=50)
        bids = orderbook['bids']
        asks = orderbook['asks']
        
        liquidity_levels = []
        
        ask_levels = {}
        for price, qty in asks:
            price_key = round(float(price), 2)
            ask_levels[price_key] = ask_levels.get(price_key, 0) + float(qty)
        
        sorted_asks = sorted(ask_levels.items(), key=lambda x: x[1], reverse=True)
        liquidity_levels.extend([price for price, qty in sorted_asks[:3]])
        
        bid_levels = {}
        for price, qty in bids:
            price_key = round(float(price), 2)
            bid_levels[price_key] = bid_levels.get(price_key, 0) + float(qty)
        
        sorted_bids = sorted(bid_levels.items(), key=lambda x: x[1], reverse=True)
        liquidity_levels.extend([price for price, qty in sorted_bids[:3]])
        
        return liquidity_levels
    
    except Exception as e:
        logger.error(f"订单簿分析失败: {str(e)}")
        return []

def calculate_ema(data, period):
    """指数移动平均计算"""
    if len(data) < period:
        return np.nan
    
    alpha = 2 / (period + 1)
    weights = np.array([(1 - alpha) ** i for i in range(period)][::-1])
    weights /= weights.sum()
    
    return np.dot(data[-period:], weights)

def calculate_bollinger(data, period=20, num_std=2):
    """布林带计算"""
    if len(data) < period:
        return np.nan, np.nan
    
    ma = np.mean(data[-period:])
    std = np.std(data[-period:])
    
    upper = ma + (std * num_std)
    lower = ma - (std * num_std)
    return upper, lower

def calculate_resistance_levels(symbol):
    try:
        logger.info(f"📊 计算阻力位: {symbol}")
        now = time.time()
        
        cache_key = f"{symbol}_resistance"
        if cache_key in resistance_cache:
            cache_data = resistance_cache[cache_key]
            if cache_data['expiration'] > now:
                logger.debug(f"📊 使用缓存的阻力位数据: {symbol}")
                return cache_data['levels']
        
        if client is None and not init_client():
            logger.error("❌ 无法初始化Binance客户端，无法计算阻力位")
            return {'resistance': {}, 'support': {}, 'current_price': 0}
        
        try:
            ticker = client.futures_symbol_ticker(symbol=symbol)
            current_price = float(ticker['price'])
            logger.info(f"📊 {symbol}当前价格: {current_price}")
        except Exception as e:
            logger.error(f"❌ 获取{symbol}当前价格失败: {str(e)}")
            current_price = None
        
        interval_levels = {}
        volume_profile_levels = add_volume_profile(symbol, '1d')
        orderbook_levels = get_orderbook_liquidity(symbol)
        
        for interval in RESISTANCE_INTERVALS:
            try:
                logger.info(f"📊 获取K线数据: {symbol} {interval}")
                klines = client.futures_klines(symbol=symbol, interval=interval, limit=100)
                
                if not klines or len(klines) < 50:
                    logger.warning(f"⚠️ {symbol}在{interval}的K线数据不足")
                    continue

                high_prices = [float(k[2]) for k in klines]
                low_prices = [float(k[3]) for k in klines]
                close_prices = [float(k[4]) for k in klines]
                open_prices = [float(k[1]) for k in klines]
                
                closes = np.array(close_prices)
                
                # 使用内置函数计算技术指标
                logger.info("📊 使用内置函数计算技术指标")
                ema50 = calculate_ema(closes, 50)
                ema100 = calculate_ema(closes, 100)
                ema200 = calculate_ema(closes, 200)
                bb_upper, bb_lower = calculate_bollinger(closes, 20, 2)
                
                recent_high = max(high_prices)
                recent_low = min(low_prices)
                fib_618 = recent_high - (recent_high - recent_low) * 0.618
                fib_other = [
                    recent_high - (recent_high - recent_low) * r 
                    for r in [0.236, 0.382, 0.5, 0.786]
                ]
                
                levels_with_type = []
                levels_with_type.append(('fib_0.618', fib_618))
                for level in fib_other:
                    levels_with_type.append(('fib_other', level))
                
                levels_with_type.append(('ema_50', ema50))
                levels_with_type.append(('ema_100', ema100))
                levels_with_type.append(('ema_200', ema200))
                levels_with_type.append(('bb_upper', bb_upper))
                levels_with_type.append(('bb_lower', bb_lower))
                
                # 修复括号错误
                if current_price and current_price > 0:
                    try:
                        # 修复括号问题
                        exponent = math.floor(math.log10(current_price))
                        adjusted_exponent = max(0, exponent - 1)
                        base = 10 ** adjusted_exponent
                        integer_level = round(current_price / base) * base
                        levels_with_type.append(('integer', integer_level))
                    except Exception as e:
                        logger.error(f"计算心理整数位失败: {str(e)}")
                
                for level in volume_profile_levels:
                    levels_with_type.append(('volume_profile', level))
                
                for level in orderbook_levels:
                    levels_with_type.append(('orderbook', level))
                
                level_scores = {}
                tolerance = current_price * 0.005 if current_price else 0.01
                
                for level_type, level_value in levels_with_type:
                    level_value = round(level_value, 4)
                    if level_value not in level_scores:
                        level_scores[level_value] = {
                            'score': 0.0,
                            'sources': set()
                        }
                    level_scores[level_value]['sources'].add(level_type)
                
                for i, (level_type1, level_value1) in enumerate(levels_with_type):
                    level_value1 = round(level_value1, 4)
                    for j, (level_type2, level_value2) in enumerate(levels_with_type):
                        if i == j: 
                            continue
                        if abs(level_value1 - level_value2) <= tolerance:
                            weight1 = INDICATOR_WEIGHTS.get(level_type1, 1.0)
                            weight2 = INDICATOR_WEIGHTS.get(level_type2, 1.0)
                            level_scores[level_value1]['score'] += weight1 * weight2
                
                merged_levels = []
                sorted_levels = sorted(level_scores.keys())
                i = 0
                while i < len(sorted_levels):
                    current = sorted_levels[i]
                    group = [current]
                    j = i + 1
                    while j < len(sorted_levels) and sorted_levels[j] - current <= tolerance:
                        group.append(sorted_levels[j])
                        j += 1
                    
                    best_level = max(group, key=lambda x: level_scores[x]['score'])
                    merged_sources = set()
                    for level in group:
                        merged_sources |= level_scores[level]['sources']
                    
                    strength = min(1.0, level_scores[best_level]['score'] / 30.0)
                    
                    # 过滤掉强度为0的级别
                    if strength > 0:
                        merged_levels.append({
                            'price': best_level,
                            'strength': strength,
                            'sources': len(merged_sources),
                            'source_types': list(merged_sources)
                        })
                    i = j
                
                merged_levels.sort(key=lambda x: x['strength'], reverse=True)
                
                resistance = []
                support = []
                
                for level in merged_levels:
                    price = level['price']
                    if current_price and price > current_price:
                        distance_percent = (price - current_price) / current_price * 100
                        resistance.append({
                            'price': price,
                            'strength': round(level['strength'], 2),
                            'distance_percent': round(distance_percent, 2),
                            'sources': level['sources'],
                            'source_types': level['source_types']
                        })
                    elif current_price and price < current_price:
                        distance_percent = (price - current_price) / current_price * 100
                        support.append({
                            'price': price,
                            'strength': round(level['strength'], 2),
                            'distance_percent': round(distance_percent, 2),
                            'sources': level['sources'],
                            'source_types': level['source_types']
                        })
                
                resistance.sort(key=lambda x: x['strength'], reverse=True)
                support.sort(key=lambda x: x['strength'], reverse=True)
                
                # 只保留前3个阻力位和支撑位
                interval_levels[interval] = {
                    'resistance': resistance[:3],
                    'support': support[:3]
                }
                
                logger.info(f"📊 {symbol}在{interval}的有效阻力位: {resistance[:3]}, 支撑位: {support[:3]}")
                
            except Exception as e:
                logger.error(f"计算{symbol}在{interval}的阻力位失败: {str(e)}")
                logger.error(traceback.format_exc())

        levels = {
            'levels': interval_levels,
            'current_price': current_price or 0,
            'volume_profile_levels': volume_profile_levels,
            'orderbook_levels': orderbook_levels
        }
        
        resistance_cache[cache_key] = {
            'levels': levels,
            'expiration': now + RESISTANCE_CACHE_EXPIRATION
        }
        return levels
    except Exception as e:
        logger.error(f"计算{symbol}的阻力位失败: {str(e)}")
        logger.error(traceback.format_exc())
        return {'levels': {}, 'current_price': 0}

def analyze_symbol(symbol):
    try:
        logger.info(f"🔍 开始分析币种: {symbol}")
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
                logger.info(f"📊 {symbol} 日线上涨条件满足，涨幅: {daily_change:.2f}%")
                
                daily_rising_item = {
                    'symbol': symbol,
                    'oi': daily_series[-1],
                    'change': round(daily_change, 2),
                    'period_status': symbol_result['period_status'].copy()
                }
                symbol_result['daily_rising'] = daily_rising_item
                symbol_result['period_count'] = 1

                logger.info(f"📊 开始全周期分析: {symbol}")
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
                    logger.info(f"📊 {symbol} 全周期上涨条件满足")
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
                logger.debug(f"📊 {symbol} 短期活跃比率: {ratio:.2f}")
                
                if ratio > 1.5:
                    logger.info(f"📊 {symbol} 短期活跃条件满足")
                    symbol_result['short_term_active'] = {
                        'symbol': symbol,
                        'oi': min5_series[-1],
                        'ratio': round(ratio, 2)
                    }

        logger.info(f"✅ 完成分析币种: {symbol}")
        return symbol_result
    except Exception as e:
        logger.error(f"❌ 处理{symbol}时出错: {str(e)}")
        logger.error(traceback.format_exc())
        return {
            'symbol': symbol,
            'period_status': {p: False for p in VALID_PERIODS},
            'period_count': 0
        }

def analyze_trends():
    start_time = time.time()
    logger.info("🔍 开始分析币种趋势...")
    symbols = get_high_volume_symbols()
    
    if not symbols:
        logger.warning("⚠️ 没有找到高交易量币种")
        return data_cache

    logger.info(f"🔍 开始分析 {len(symbols)} 个币种")

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
    logger.info(f"📊 分析结果: 日线上涨 {len(daily_rising)}个, 短期活跃 {len(short_term_active)}个, 全部周期上涨 {len(all_cycle_rising)}个")
    logger.info(f"✅ 分析完成: 用时{analysis_time:.2f}秒")

    tz_shanghai = timezone(timedelta(hours=8))
    return {
        'daily_rising': daily_rising,
        'short_term_active': short_term_active,
        'all_cycle_rising': all_cycle_rising,
        'analysis_time': analysis_time,
        'last_updated': datetime.now(tz_shanghai).strftime("%Y-%m-%d %H:%M:%S"),
        'next_analysis_time': get_next_update_time('5m').strftime("%Y-%m-%d %H:%M:%S")
    }

def get_high_volume_symbols():
    if client is None and not init_client():
        logger.error("❌ 无法连接API")
        return []

    try:
        logger.info("📊 获取高交易量币种...")
        tickers = client.futures_ticker()
        filtered = [
            t for t in tickers if float(t.get('quoteVolume', 0)) > 10000000
            and t.get('symbol', '').endswith('USDT')
        ]
        logger.info(f"📊 找到 {len(filtered)} 个高交易量币种")
        return [t['symbol'] for t in filtered]
    except Exception as e:
        logger.error(f"❌ 获取高交易量币种失败: {str(e)}")
        logger.error(traceback.format_exc())
        return []

def analysis_worker():
    global data_cache, current_data_cache
    logger.info("🔧 数据分析线程启动")

    data_cache = {
        "last_updated": "从未更新",
        "daily_rising": [],
        "short_term_active": [],
        "all_cycle_rising": [],
        "analysis_time": 0,
        "next_analysis_time": "计算中..."
    }
    current_data_cache = data_cache.copy()

    while True:
        try:
            task = analysis_queue.get()
            if task == "STOP":
                logger.info("🛑 收到停止信号，结束分析线程")
                analysis_queue.task_done()
                break

            analysis_start = datetime.now(timezone.utc)
            logger.info(f"⏱️ 开始更新数据 ({analysis_start.strftime('%Y-%m-%d %H:%M:%S')})...")

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
                
                logger.info(f"📊 分析结果已生成")
                logger.info(f"全周期上涨币种数量: {len(new_data['all_cycle_rising'])}")
                logger.info(f"日线上涨币种数量: {len(new_data['daily_rising'])}")
                logger.info(f"短期活跃币种数量: {len(new_data['short_term_active'])}")
                
                data_cache = new_data
                current_data_cache = new_data.copy()
                logger.info(f"✅ 数据更新成功")
            except Exception as e:
                logger.error(f"❌ 分析过程中出错: {str(e)}")
                logger.error(traceback.format_exc())
                data_cache = backup_cache
                current_data_cache = current_backup
                logger.info("🔄 恢复历史数据")

            analysis_end = datetime.now(timezone.utc)
            analysis_duration = (analysis_end - analysis_start).total_seconds()
            logger.info(f"⏱️ 分析耗时: {analysis_duration:.2f}秒")
            
            next_time = get_next_update_time('5m')
            wait_seconds = (next_time - analysis_end).total_seconds()
            logger.info(f"⏳ 下次分析将在 {wait_seconds:.1f} 秒后 ({next_time.strftime('%Y-%m-%d %H:%M:%S')})")
            
            logger.info("=" * 50)
            
            analysis_queue.task_done()
        except Exception as e:
            logger.error(f"❌ 分析失败: {str(e)}")
            logger.error(traceback.format_exc())
            analysis_queue.task_done()

def schedule_analysis():
    logger.info("⏰ 定时分析调度器启动")
    now = datetime.now(timezone.utc)
    next_time = get_next_update_time('5m')
    initial_wait = (next_time - now).total_seconds()
    logger.info(f"⏳ 首次分析将在 {initial_wait:.1f} 秒后开始 ({next_time.strftime('%Y-%m-%d %H:%M:%S')})...")
    time.sleep(max(0, initial_wait))

    while True:
        analysis_start = datetime.now(timezone.utc)
        logger.info(f"🔔 触发定时分析任务 ({analysis_start.strftime('%Y-%m-%d %H:%M:%S')}")
        analysis_queue.put("ANALYZE")
        analysis_queue.join()

        analysis_duration = (datetime.now(timezone.utc) - analysis_start).total_seconds()
        now = datetime.now(timezone.utc)
        next_time = get_next_update_time('5m')
        wait_time = (next_time - now).total_seconds()

        if wait_time < 0:
            wait_time = 0
        elif analysis_duration > 240:
            wait_time = 0
            logger.warning("⚠️ 分析耗时过长，立即开始下一次分析")
        elif wait_time > 300:
            adjusted_wait = max(60, wait_time - 120)
            logger.info(f"⏳ 调整等待时间: {wait_time:.1f}秒 -> {adjusted_wait:.1f}秒")
            wait_time = adjusted_wait

        logger.info(f"⏳ 下次分析将在 {wait_time:.1f} 秒后 ({next_time.strftime('%Y-%m-%d %H:%M:%S')})")
        time.sleep(wait_time)

# API路由
@app.route('/')
def index():
    try:
        return send_from_directory(app.static_folder, 'index.html')
    except Exception as e:
        logger.error(f"❌ 处理首页请求失败: {str(e)}")
        return "Internal Server Error", 500

@app.route('/<path:filename>')
def static_files(filename):
    return send_from_directory(app.static_folder, filename)

@app.route('/api/data', methods=['GET'])
def get_data():
    global current_data_cache
    try:
        logger.info("📡 收到 /api/data 请求")
        
        # 强制刷新数据如果缓存为空
        if not current_data_cache or current_data_cache.get("last_updated") == "从未更新":
            logger.info("🔄 缓存为空，触发即时分析")
            analysis_queue.put("ANALYZE")
            return jsonify({
                'last_updated': "数据生成中...",
                'daily_rising': [],
                'short_term_active': [],
                'all_cycle_rising': [],
                'analysis_time': 0,
                'next_analysis_time': "计算中..."
            })
        
        if not current_data_cache or not isinstance(current_data_cache, dict):
            logger.warning("⚠️ 当前数据缓存格式错误，重置为默认")
            current_data_cache = {
                "last_updated": "从未更新",
                "daily_rising": [],
                "short_term_active": [],
                "all_cycle_rising": [],
                "analysis_time": 0,
                "next_analysis_time": "计算中..."
            }
        
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
        
        logger.info(f"📦 返回数据: 日线上涨 {len(filtered_daily_rising)}个, 全周期上涨 {len(all_cycle_rising)}个")
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
            logger.warning(f"⚠️ 无效的币种名称: {symbol}")
            return jsonify({'error': 'Invalid symbol format'}), 400

        logger.info(f"📊 获取阻力位数据: {symbol}")
        levels = calculate_resistance_levels(symbol)
        
        if not isinstance(levels, dict):
            logger.error(f"❌ 阻力位数据结构错误: {type(levels)}")
            return jsonify({'error': 'Invalid resistance data structure'}), 500
        
        if 'levels' not in levels:
            levels['levels'] = {}
        if 'current_price' not in levels:
            levels['current_price'] = 0
        
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
            logger.warning(f"⚠️ 无效的币种名称: {symbol}")
            return jsonify({'error': 'Invalid symbol format'}), 400

        if period not in VALID_PERIODS:
            logger.warning(f"⚠ 不支持的周期: {period}")
            return jsonify({'error': 'Unsupported period'}), 400

        logger.info(f"📈 获取持仓量图表数据: symbol={symbol}, period={period}")
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
        binance_status = 'ok'
        if client:
            try:
                client.get_server_time()
            except:
                binance_status = 'error'
        else:
            binance_status = 'not initialized'
        
        tz_shanghai = timezone(timedelta(hours=8))
        return jsonify({
            'status': 'healthy',
            'binance': binance_status,
            'last_updated': current_data_cache.get('last_updated', 'N/A'),
            'next_analysis_time': current_data_cache.get('next_analysis_time', 'N/A'),
            'server_time': datetime.now(tz_shanghai).strftime("%Y-%m-%d %H:%M:%S")
        })
    except Exception as e:
        return jsonify({
            'status': 'unhealthy',
            'error': str(e)
        }), 500

def start_background_threads():
    static_path = app.static_folder
    if not os.path.exists(static_path):
        os.makedirs(static_path)
    
    index_path = os.path.join(static_path, 'index.html')
    if not os.path.exists(index_path):
        with open(index_path, 'w') as f:
            f.write("<html><body><h1>请将前端文件放入static目录</h1></body></html>")
    
    if not init_client():
        logger.critical("❌ 无法初始化客户端")
        return False
    
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
    logger.info("🆕 创建初始内存数据记录")
    
    worker_thread = threading.Thread(target=analysis_worker, name="AnalysisWorker")
    worker_thread.daemon = True
    worker_thread.start()
    
    scheduler_thread = threading.Thread(target=schedule_analysis, name="AnalysisScheduler")
    scheduler_thread.daemon = True
    scheduler_thread.start()
    
    # 添加初始分析任务
    analysis_queue.put("ANALYZE")
    logger.info("🔄 已提交初始分析任务")
    
    logger.info("✅ 后台线程启动成功")
    return True

if __name__ == '__main__':
    PORT = int(os.environ.get("PORT", 9600))
    
    logger.info("=" * 50)
    logger.info(f"🚀 启动加密货币持仓量分析服务")
    logger.info(f"🔑 API密钥: {API_KEY[:5]}...{API_KEY[-3:] if API_KEY else '未设置'}")
    logger.info(f"🌐 服务端口: {PORT}")
    logger.info("💾 数据存储: 内存存储 (无持久化)")
    logger.info("=" * 50)
    
    if start_background_threads():
        logger.info("🚀 启动服务器...")
        app.run(host='0.0.0.0', port=PORT, debug=False)
    else:
        logger.critical("🔥 无法启动服务，请检查错误日志")
