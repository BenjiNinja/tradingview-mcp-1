#!/usr/bin/env python3
"""
TradingView MCP Server - WORKING VERSION
Uses Playwright for lightweight browser automation (required because TradingView renders charts client-side).
Optimized with browser reuse and cookie-based authentication for minimal resource usage.
"""

import os
import sys
import json
import base64
import logging
import asyncio
from typing import Optional
from pathlib import Path

# Ensure package imports work when launched as a script (MCP stdio)
_SRC_ROOT = Path(__file__).resolve().parent.parent
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

# Wrap setup and imports in a try/except block to capture and write any critical errors
# directly to the supervisor log file, avoiding silent subprocess crashes.
try:
    from dotenv import load_dotenv
    from playwright.async_api import async_playwright, Browser, BrowserContext
    from mcp.server import Server
    from mcp.types import Tool, TextContent, ImageContent
    from mcp.server.stdio import stdio_server

    from tradingview_mcp.redis_utils import (
        publish_live_state,
        read_live_state,
        fetch_active_positions,
        bot_symbol,
    )
    
    # Configure logging
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)
    
    # Load environment variables
    load_dotenv()
except Exception as startup_err:
    import traceback
    from datetime import datetime
    sys.stderr.write(f"CRITICAL: Failed to import dependencies or initialize in server.py: {startup_err}\n")
    traceback.print_exc(file=sys.stderr)
    try:
        mcp_dir = Path(__file__).resolve().parent
        current = mcp_dir
        data_dir = None
        for _ in range(5):
            if (current / "data").exists():
                data_dir = current / "data"
                break
            current = current.parent
        if not data_dir:
            data_dir = Path(__file__).resolve().parents[3] / "data"
        
        data_dir.mkdir(parents=True, exist_ok=True)
        log_file = data_dir / "claude_scan.log"
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"\n[{datetime.now().isoformat()}] === MCP SERVER IMPORT/STARTUP ERROR ===\n")
            traceback.print_exc(file=f)
            f.write("=========================================\n")
    except Exception as log_err:
        sys.stderr.write(f"Failed to log to data file: {log_err}\n")
    sys.exit(1)

# Global browser instance (reused for efficiency)
_playwright = None
_browser: Optional[Browser] = None
_context: Optional[BrowserContext] = None


async def get_browser_context() -> BrowserContext:
    """Get or create a persistent browser context with TradingView authentication."""
    global _playwright, _browser, _context
    
    if _context is not None:
        return _context
    
    session_id = os.getenv("TRADINGVIEW_SESSION_ID")
    session_id_sign = os.getenv("TRADINGVIEW_SESSION_ID_SIGN")
    
    if not session_id or not session_id_sign:
        raise ValueError("TradingView credentials not found in environment")
    
    # Start Playwright
    try:
        if _playwright is None:
            _playwright = await async_playwright().start()
    except Exception as e:
        logger.error(f"FATAL: Playwright start failed: {e}", exc_info=True)
        sys.stderr.write(f"FATAL: Playwright start failed: {e}\n")
        sys.stderr.flush()
        raise
    
    # Launch browser in headless mode (lightweight)
    if _browser is None:
        try:
            _browser = await _playwright.chromium.launch(
                headless=True,
                args=[
                    '--no-sandbox',
                    '--disable-setuid-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-accelerated-2d-canvas',
                    '--no-first-run',
                    '--no-zygote',
                    '--disable-gpu'
                ]
            )
            logger.info("Browser launched successfully")
        except Exception as e:
            logger.error(f"FATAL: Chromium launch failed: {e}", exc_info=True)
            sys.stderr.write(f"FATAL: Chromium launch failed: {e}\n")
            sys.stderr.flush()
            raise

    
    # Create context with cookies
    _context = await _browser.new_context(
        viewport={'width': 1920, 'height': 1080},
        user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    )
    
    # Add TradingView cookies
    await _context.add_cookies([
        {
            'name': 'sessionid',
            'value': session_id,
            'domain': '.tradingview.com',
            'path': '/',
            'httpOnly': True,
            'secure': True,
            'sameSite': 'Lax'
        },
        {
            'name': 'sessionid_sign',
            'value': session_id_sign,
            'domain': '.tradingview.com',
            'path': '/',
            'httpOnly': True,
            'secure': True,
            'sameSite': 'Lax'
        }
    ])
    
    logger.info("Browser context created with authentication")
    return _context


async def get_chart_snapshot(
    symbol: str,
    interval: str = "D",
    width: int = 1200,
    height: int = 600,
    theme: str = "dark"
) -> Optional[bytes]:
    """
    Fetch a TradingView chart snapshot.
    
    Args:
        symbol: Trading symbol (e.g., "BINANCE:BTCUSDT")
        interval: Chart interval (1, 5, 15, 30, 60, 240, D, W, M)
        width: Image width
        height: Image height
        theme: Chart theme (dark or light)
    
    Returns:
        PNG image bytes or None if failed
    """
    page = None
    try:
        context = await get_browser_context()
        page = await context.new_page()
        
        # Set viewport
        await page.set_viewport_size({"width": width, "height": height})
        
        # Build TradingView chart URL
        chart_url = (
            f"https://www.tradingview.com/chart/?symbol={symbol}"
            f"&interval={interval}"
            f"&theme={theme}"
        )
        
        logger.info(f"Loading chart: {symbol} ({interval})")
        
        # Navigate to chart
        try:
            # Try with domcontentloaded first (faster)
            await page.goto(chart_url, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            # If it's a timeout or something that isn't a hard crash, try one more time with 'load'
            err_msg = str(e).lower()
            if "interrupted" in err_msg or "closed" in err_msg:
                 logger.error(f"Critical navigation error for {symbol}: {e}")
                 raise # Re-raise to be caught by outer try and close page
            
            logger.warning(f"Initial navigation for {symbol} failed/timed out: {e}. Trying fallback...")
            # Brief pause before retry
            await asyncio.sleep(2)
            await page.goto(chart_url, wait_until="load", timeout=45000)
        
        # Wait for chart to load (with fallback)
        try:
            # The legend item is a good indicator the chart has data
            await page.wait_for_selector('div[data-name="legend-source-item"]', timeout=20000)
        except Exception as e:
            logger.debug(f"Legend selector not found for {symbol}, trying container: {e}")
            try:
                await page.wait_for_selector('.chart-container', timeout=10000)
            except:
                logger.warning(f"No known chart selectors found for {symbol}, proceeding to screenshot anyway.")
        
        # Additional wait for actual candle rendering
        await asyncio.sleep(5)
        
        # Take screenshot
        screenshot = await page.screenshot(type='png', full_page=False)
        logger.info(f"Screenshot captured: {len(screenshot)} bytes")
        
        return screenshot
        
    except Exception as e:
        logger.error(f"Failed to capture chart {symbol}: {e}")
        return None
    finally:
        if page:
            try:
                await page.close()
            except:
                pass


async def validate_session() -> bool:
    """Validate if TradingView session is working."""
    page = None
    try:
        context = await get_browser_context()
        page = await context.new_page()
        
        await page.goto("https://www.tradingview.com/", timeout=15000)
        
        # Check if we're logged in (look for user menu or profile indicators)
        await asyncio.sleep(1)
        content = await page.content()
        
        # If we see login/signin, we're NOT authenticated
        is_authenticated = 'sign in' not in content.lower() or 'user-menu' in content.lower()
        
        return is_authenticated
        
    except Exception as e:
        logger.error(f"Session validation failed: {e}")
        return False
    finally:
        if page:
            try:
                await page.close()
            except:
                pass


async def cleanup():
    """Cleanup browser resources."""
    global _browser, _context, _playwright
    
    if _context:
        await _context.close()
        _context = None
    
    if _browser:
        await _browser.close()
        _browser = None
    
    if _playwright:
        await _playwright.stop()
        _playwright = None


# Initialize MCP server
app = Server("tradingview-mcp")


@app.list_tools()
async def list_tools() -> list[Tool]:
    """List available MCP tools."""
    return [
        Tool(
            name="get_chart_snapshot",
            description="Fetch a TradingView chart snapshot for a given symbol and timeframe. "
                       "Returns the chart as a base64-encoded PNG image. "
                       "Symbol format: 'EXCHANGE:SYMBOL' (e.g., 'BINANCE:BTCUSDT', 'NASDAQ:AAPL'). "
                       "Timeframes: 1, 5, 15, 30, 60, 240 (minutes) or D, W, M (day/week/month).",
            inputSchema={
                "type": "object",
                "properties": {
                    "symbol": {
                        "type": "string",
                        "description": "Trading symbol in TradingView format (e.g., 'BINANCE:BTCUSDT')"
                    },
                    "interval": {
                        "type": "string",
                        "description": "Chart interval: 1, 5, 15, 30, 60, 240 (minutes) or D, W, M",
                        "default": "D"
                    },
                    "width": {
                        "type": "number",
                        "description": "Image width in pixels (default: 1200)",
                        "default": 1200
                    },
                    "height": {
                        "type": "number",
                        "description": "Image height in pixels (default: 600)",
                        "default": 600
                    },
                    "theme": {
                        "type": "string",
                        "description": "Chart theme: 'dark' or 'light' (default: dark)",
                        "default": "dark",
                        "enum": ["dark", "light"]
                    }
                },
                "required": ["symbol"]
            }
        ),
        Tool(
            name="validate_session",
            description="Validate if the TradingView session credentials are working correctly.",
            inputSchema={
                "type": "object",
                "properties": {}
            }
        ),
        Tool(
            name="list_timeframes",
            description="List all available timeframes/intervals for TradingView charts.",
            inputSchema={
                "type": "object",
                "properties": {}
            }
        ),
        Tool(
            name="get_multi_timeframe_snapshot",
            description="Fetch multiple TradingView chart snapshots for a symbol at different timeframes. "
                        "Returns a list of base64-encoded PNG images. Useful for top-down market structure analysis.",
            inputSchema={
                "type": "object",
                "properties": {
                    "symbol": {
                        "type": "string",
                        "description": "Trading symbol in TradingView format (e.g., 'BINANCE:BTCUSDT')"
                    },
                    "intervals": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of intervals to capture (e.g., ['60', '240', 'D'])",
                        "default": ["60", "240", "D"]
                    },
                    "theme": {
                        "type": "string",
                        "description": "Chart theme: 'dark' or 'light' (default: dark)",
                        "default": "dark",
                        "enum": ["dark", "light"]
                    }
                },
                "required": ["symbol", "intervals"]
            }
        ),
        Tool(
            name="publish_bias_state",
            description="Publish market regime, bias, structure, and optional position review to Redis "
                       "(key: claude:live_state:{symbol}). Normalizes bias to LONG_ONLY/SHORT_ONLY/BOTH/NO_TRADE.",
            inputSchema={
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "TV or bot symbol (e.g. BINANCE:BTCUSDT or BTCUSDT)"},
                    "regime": {"type": "string", "description": "BULLISH | BEARISH | RANGING (aliases normalized)"},
                    "recommended_bias": {"type": "string", "description": "LONG_ONLY | SHORT_ONLY | BOTH | NO_TRADE"},
                    "confidence": {"type": "number", "description": "0.0–1.0 or 0–100 (auto-normalized)"},
                    "reasoning": {"type": "string", "description": "Brief reasoning"},
                    "ttl_sec": {"type": "number", "description": "Redis TTL seconds (default 28800)", "default": 28800},
                    "market_structure": {"type": "object", "description": "Optional {trend, last_bos_direction, ...}"},
                    "chop_trap": {"type": "object", "description": "Optional {is_choppy, is_trap_session, severity, notes}"},
                    "position_review": {"type": "object", "description": "Optional {verdict, reason} for open holds"},
                },
                "required": ["symbol", "regime", "recommended_bias", "confidence"],
            },
        ),
        Tool(
            name="get_market_structure",
            description="Return market structure (HH_HL / LH_LL / CHOP) from the latest claude:live_state Redis key.",
            inputSchema={
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Trading symbol"},
                },
                "required": ["symbol"],
            },
        ),
        Tool(
            name="detect_chop_or_trap",
            description="Return chop/trap flags from the latest supervisor state in Redis.",
            inputSchema={
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Trading symbol"},
                },
                "required": ["symbol"],
            },
        ),
        Tool(
            name="get_active_position_review",
            description="Fetch open live/shadow positions from bot Redis (state:{symbol}:positions) "
                       "with age, entry, and unrealized PnL%% for supervisor prompts.",
            inputSchema={
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Bot symbol (e.g. BTCUSDT)"},
                },
                "required": ["symbol"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent | ImageContent]:
    """Handle tool calls."""
    
    # Check credentials
    session_id = os.getenv("TRADINGVIEW_SESSION_ID")
    session_id_sign = os.getenv("TRADINGVIEW_SESSION_ID_SIGN")
    
    if not session_id or not session_id_sign:
        return [TextContent(
            type="text",
            text="Error: TradingView credentials not found. Please set TRADINGVIEW_SESSION_ID and "
                 "TRADINGVIEW_SESSION_ID_SIGN in your .env file."
        )]
    
    if name == "validate_session":
        is_valid = await validate_session()
        status = "✓ Valid" if is_valid else "✗ Invalid"
        return [TextContent(
            type="text",
            text=f"Session Status: {status}\n\n"
                 f"The TradingView session credentials are {'working correctly' if is_valid else 'not working'}."
        )]
    
    elif name == "list_timeframes":
        timeframes = {
            "Minutes": ["1", "5", "15", "30", "60", "240"],
            "Days/Weeks/Months": ["D", "W", "M"]
        }
        
        result = "Available TradingView Timeframes:\n\n"
        for category, intervals in timeframes.items():
            result += f"{category}:\n"
            for interval in intervals:
                result += f"  - {interval}\n"
        
        result += "\nExamples:\n"
        result += "  - '5' = 5-minute chart\n"
        result += "  - '60' = 1-hour chart\n"
        result += "  - 'D' = Daily chart\n"
        
        return [TextContent(type="text", text=result)]
    
    elif name == "get_chart_snapshot":
        symbol = arguments.get("symbol")
        interval = arguments.get("interval", "D")
        width = int(arguments.get("width", 1200))
        height = int(arguments.get("height", 600))
        theme = arguments.get("theme", "dark")
        
        if not symbol:
            return [TextContent(
                type="text",
                text="Error: 'symbol' parameter is required. Example: 'BINANCE:BTCUSDT'"
            )]
        
        logger.info(f"Fetching chart snapshot for {symbol} ({interval})")
        
        # Get the chart snapshot
        image_data = await get_chart_snapshot(symbol, interval, width, height, theme)
        
        if image_data:
            # Encode as base64
            image_base64 = base64.b64encode(image_data).decode('utf-8')
            
            return [
                TextContent(
                    type="text",
                    text=f"Chart snapshot for {symbol} (Interval: {interval})\n"
                         f"Size: {width}x{height} | Theme: {theme}\n"
                         f"Image size: {len(image_data)} bytes"
                ),
                ImageContent(
                    type="image",
                    data=image_base64,
                    mimeType="image/png"
                )
            ]
        else:
            return [TextContent(
                type="text",
                text=f"Failed to fetch chart snapshot for {symbol}.\n\n"
                     f"Possible reasons:\n"
                     f"1. Invalid symbol format (use 'EXCHANGE:SYMBOL' format)\n"
                     f"2. Symbol not found on TradingView\n"
                     f"3. Network or timeout issues\n\n"
                     f"Please check your input and try again."
            )]
            
    elif name == "get_multi_timeframe_snapshot":
        symbol = arguments.get("symbol")
        intervals = arguments.get("intervals", ["60", "240", "D"])
        theme = arguments.get("theme", "dark")
        
        if not symbol or not intervals:
            return [TextContent(type="text", text="Error: 'symbol' and 'intervals' parameters are required.")]
            
        logger.info(f"Fetching multi-timeframe snapshots for {symbol}: {intervals}")
        
        results = []
        for interval in intervals:
            image_data = await get_chart_snapshot(symbol, interval, 1200, 600, theme)
            if image_data:
                image_base64 = base64.b64encode(image_data).decode('utf-8')
                results.append(TextContent(type="text", text=f"Snapshot for {symbol} (Interval: {interval})"))
                results.append(ImageContent(type="image", data=image_base64, mimeType="image/png"))
            else:
                results.append(TextContent(type="text", text=f"Failed to capture {interval} snapshot for {symbol}."))
                
        return results

    elif name == "publish_bias_state":
        raw_symbol = arguments.get("symbol", "")
        regime = arguments.get("regime")
        bias = arguments.get("recommended_bias")
        confidence = arguments.get("confidence")
        if not raw_symbol or not regime or not bias or confidence is None:
            return [TextContent(type="text", text="Error: Missing required fields for publish_bias_state.")]

        try:
            ttl = int(arguments.get("ttl_sec", 8 * 3600))
            sym = publish_live_state(
                raw_symbol,
                {
                    "regime": regime,
                    "recommended_bias": bias,
                    "confidence": confidence,
                    "reasoning": arguments.get("reasoning", ""),
                    "market_structure": arguments.get("market_structure"),
                    "chop_trap": arguments.get("chop_trap"),
                    "position_review": arguments.get("position_review"),
                },
                ttl_sec=ttl,
            )
            return [TextContent(
                type="text",
                text=f"Published claude:live_state:{sym} (TTL={ttl}s)",
            )]
        except Exception as e:
            logger.error(f"Failed to publish bias state: {e}")
            return [TextContent(type="text", text=f"Failed to publish to Redis: {e}")]

    elif name == "get_market_structure":
        symbol = arguments.get("symbol", "")
        if not symbol:
            return [TextContent(type="text", text="Error: symbol is required.")]
        try:
            state = read_live_state(symbol)
            if not state:
                return [TextContent(type="text", text=json.dumps({
                    "symbol": bot_symbol(symbol),
                    "status": "unavailable",
                    "market_structure": {"trend": "CHOP"},
                }))]
            ms = state.get("market_structure", {"trend": "CHOP"})
            return [TextContent(type="text", text=json.dumps({
                "symbol": bot_symbol(symbol),
                "status": "ok",
                "market_structure": ms,
                "_published_at": state.get("_published_at"),
            }))]
        except Exception as e:
            return [TextContent(type="text", text=f"Error reading market structure: {e}")]

    elif name == "detect_chop_or_trap":
        symbol = arguments.get("symbol", "")
        if not symbol:
            return [TextContent(type="text", text="Error: symbol is required.")]
        try:
            state = read_live_state(symbol)
            if not state:
                return [TextContent(type="text", text=json.dumps({
                    "symbol": bot_symbol(symbol),
                    "status": "unavailable",
                    "is_choppy": False,
                    "is_trap_session": False,
                }))]
            chop = state.get("chop_trap", {})
            return [TextContent(type="text", text=json.dumps({
                "symbol": bot_symbol(symbol),
                "status": "ok",
                "is_choppy": bool(chop.get("is_choppy", False)),
                "is_trap_session": bool(chop.get("is_trap_session", False)),
                "severity": chop.get("severity", "LOW"),
                "notes": chop.get("notes", ""),
                "regime": state.get("regime"),
                "_published_at": state.get("_published_at"),
            }))]
        except Exception as e:
            return [TextContent(type="text", text=f"Error detecting chop/trap: {e}")]

    elif name == "get_active_position_review":
        symbol = arguments.get("symbol", "")
        if not symbol:
            return [TextContent(type="text", text="Error: symbol is required.")]
        try:
            import json as _json
            data = fetch_active_positions(symbol)
            return [TextContent(type="text", text=_json.dumps(data, indent=2))]
        except Exception as e:
            return [TextContent(type="text", text=f"Error fetching positions: {e}")]
    
    return [TextContent(type="text", text=f"Unknown tool: {name}")]


async def main():
    """Run the MCP server."""
    logger.info("Starting TradingView MCP Server with Playwright...")
    
    # Validate environment variables
    if not os.getenv("TRADINGVIEW_SESSION_ID") or not os.getenv("TRADINGVIEW_SESSION_ID_SIGN"):
        logger.warning(
            "Warning: TradingView credentials not found in environment. "
            "Please set TRADINGVIEW_SESSION_ID and TRADINGVIEW_SESSION_ID_SIGN in .env file."
        )
    
    try:
        async with stdio_server() as (read_stream, write_stream):
            await app.run(read_stream, write_stream, app.create_initialization_options())
    finally:
        await cleanup()


if __name__ == "__main__":
    asyncio.run(main())
