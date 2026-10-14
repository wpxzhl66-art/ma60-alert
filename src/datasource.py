"""数据源抽象层 —— 统一各数据源的取数接口。

本层的每一项设计都由《数据源可用性核验报告 v1.0.0》的实测结论驱动：

1. 周线存在 264 条硬上限（约5年），无法覆盖 2010 年至今
   → 本层只取**日线**，周线由 engine 层合成。
2. `--limit N` 的语义是「返回最近 N 条」，会覆盖 `--start` 的下限约束
   → 按区间**分段取数**，段内条数可控。
3. 接口超时后**静默返回空结果**而非报错（同一段首次0条、重试3次均487条）
   → 每次请求都做**重试 + 条数校验**。
4. `--end` 传入未来日期会直接返回空
   → 统一钳制到当日，禁止未来日期。
5. 复权必须用 `hfq`。该源 `qfq` 为加法复权，2010年价格为负（-188.89）；
   省略 `--fq` 时默认口径等同于此，同样不可用。

统一输出格式（所有数据源必须遵守）：
    DataFrame，列 = [date, open, high, low, close, volume, amount]
    date 为 datetime64，按升序排列。
"""

from __future__ import annotations

import subprocess
import threading
import time
import zlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd


# --------------------------------------------------------------------------
# baostock 客户端缺陷修补（必须在使用前调用）
# --------------------------------------------------------------------------

_BS_PATCHED = False


def patch_baostock_io(timeout: float = 60.0) -> None:
    """修补 baostock 客户端的收包死循环缺陷。

    缺陷（源码 `baostock/util/socketutil.py::send_msg`）::

        receive = b""
        while True:
            recv = default_socket.recv(8192)
            receive += recv
            if receive[-13:] == b"<![CDATA[]]>\\n":
                break

    当服务端**关闭连接**时，`recv` 返回空字节 `b""`（EOF），`receive` 不再
    变化，终止条件永不成立 → **无限空转**，进程 100% CPU 且永不返回。
    更糟的是 socket 超时对这毫无作用：`recv` 是立即返回而非超时。

    这正是"被限流后查询永久挂起"的真正原因，也是批量建库无法自愈的根因。

    补丁行为：
      1. 显式识别 EOF 并抛 ConnectionError（而非静默死循环）
      2. 总时限控制，超时抛 TimeoutError
      3. 不再吞掉异常（原实现只 print 后返回 None，调用方误判为"无数据"）
    """
    global _BS_PATCHED
    if _BS_PATCHED:
        return

    import baostock.common.contants as cons
    import baostock.common.context as context
    import baostock.util.socketutil as sock_util

    def safe_send_msg(msg):
        if not hasattr(context, "default_socket"):
            raise ConnectionError("baostock: 尚未登录")
        s = getattr(context, "default_socket")
        if s is None:
            raise ConnectionError("baostock: socket 不可用")
        try:
            s.settimeout(timeout)
        except OSError:
            pass
        s.sendall(bytes(msg + "\n", encoding="utf-8"))

        buf = b""
        deadline = time.time() + timeout
        while True:
            if time.time() > deadline:
                raise TimeoutError(f"baostock: 收包超时（>{timeout:.0f}s 无响应）")
            chunk = s.recv(8192)
            if not chunk:
                raise ConnectionError("baostock: 服务端关闭连接（EOF）")
            buf += chunk
            if buf[-13:] == b"<![CDATA[]]>\n":
                break

        head_str = bytes.decode(buf[0:cons.MESSAGE_HEADER_LENGTH])
        head_arr = head_str.split(cons.MESSAGE_SPLIT)
        if head_arr[1] in cons.COMPRESSED_MESSAGE_TYPE_TUPLE:
            inner = int(head_arr[2])
            body = bytes.decode(zlib.decompress(
                buf[cons.MESSAGE_HEADER_LENGTH:cons.MESSAGE_HEADER_LENGTH + inner]))
            return head_str + body
        return bytes.decode(buf)

    sock_util.send_msg = safe_send_msg
    _BS_PATCHED = True


# --------------------------------------------------------------------------
# 接口定义
# --------------------------------------------------------------------------

class BarSource(ABC):
    """日线数据源接口。"""

    name: str = "abstract"

    @abstractmethod
    def fetch_daily(self, code: str, start: str, end: str | None = None) -> pd.DataFrame:
        """取单只标的的日线（已复权），返回标准化 DataFrame。"""

    @abstractmethod
    def list_constituents(self, index_code: str) -> pd.DataFrame:
        """取指数成分股清单，返回列 [code, name]。"""

    def fetch_daily_many(self, codes, start, end=None, verbose=True) -> dict:
        """批量取日线。逐只串行 + 间隔，避免触发接口限流。"""
        out = {}
        for i, code in enumerate(codes, 1):
            try:
                df = self.fetch_daily(code, start, end)
                if df.empty:
                    if verbose:
                        print(f"  [{i}/{len(codes)}] {code} 空数据")
                    continue
                out[code] = df
            except Exception as exc:  # noqa: BLE001
                if verbose:
                    print(f"  [{i}/{len(codes)}] {code} 失败: {exc}")
            if verbose and i % 20 == 0:
                print(f"  进度 {i}/{len(codes)}，已成功 {len(out)}")
        return out


# --------------------------------------------------------------------------
# westock 数据源
# --------------------------------------------------------------------------

@dataclass
class WestockSource(BarSource):
    """westock CLI 数据源。

    注意：该类只实现**已验证可用**的能力。市场环境、市值、行业等
    依赖 iFinD 的数据在此不提供。
    """

    exe: str = r"C:\Users\Administrator\.local\bin\westock.exe"
    adjust: str = "hfq"          # 必须 hfq，见模块文档
    limit: int = 5000
    retries: int = 4
    retry_sleep: float = 1.5
    min_rows: int = 1            # 单段最少条数，低于此值视为取数失败
    segment: str = "half_year"
    _today: str = field(default_factory=lambda: date.today().isoformat())
    name: str = "westock"

    # ---------------- 底层调用 ----------------

    def _run(self, args: list[str]) -> str:
        proc = subprocess.run(
            [self.exe, *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        return proc.stdout or ""

    @staticmethod
    def _parse_table(text: str) -> list[dict]:
        """解析 CLI 输出的 markdown 表格。列序：date open last high low volume amount ..."""
        rows = []
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("|"):
                continue
            parts = [p.strip() for p in line.strip("|").split("|")]
            if len(parts) < 8 or parts[0] == "date" or set(parts[0]) <= {"-"}:
                continue
            try:
                rows.append({
                    "date": pd.Timestamp(parts[0]),
                    "open": float(parts[1]),
                    "close": float(parts[2]),      # CLI 中 "last" 即收盘
                    "high": float(parts[3]),
                    "low": float(parts[4]),
                    "volume": float(parts[5]),
                    "amount": float(parts[6]),
                })
            except (ValueError, IndexError):
                continue
        return rows

    def _clamp_end(self, end: str | None) -> str | None:
        """禁止未来日期：接口对未来的 --end 会直接返回空。"""
        if end is None:
            return None
        e = pd.Timestamp(end)
        t = pd.Timestamp(self._today)
        return min(e, t).date().isoformat()

    def _segments(self, start: str, end: str | None):
        """把区间切成半年一段，规避单次请求截断。"""
        s = pd.Timestamp(start)
        e = pd.Timestamp(self._clamp_end(end) or self._today)
        if s > e:
            return
        cur = s
        while cur <= e:
            if self.segment == "half_year":
                nxt = pd.Timestamp(year=cur.year, month=7 if cur.month <= 6 else 12, day=1)
                if cur.month <= 6:
                    seg_end = pd.Timestamp(year=cur.year, month=6, day=30)
                else:
                    seg_end = pd.Timestamp(year=cur.year, month=12, day=31)
            elif self.segment == "year":
                seg_end = pd.Timestamp(year=cur.year, month=12, day=31)
            else:
                raise ValueError(f"未知分段方式: {self.segment}")
            yield cur.date().isoformat(), min(seg_end, e).date().isoformat()
            cur = min(seg_end, e) + pd.Timedelta(days=1)

    # ---------------- 日线取数 ----------------

    def _fetch_segment(self, code: str, start: str, end: str) -> list[dict]:
        """带重试 + 条数校验的单段取数。"""
        for attempt in range(self.retries):
            args = ["kline", code, "--period", "day", "--start", start,
                    "--limit", str(self.limit)]
            if self.adjust:
                args += ["--fq", self.adjust]
            args += ["--end", end]
            rows = self._parse_table(self._run(args))
            if len(rows) >= self.min_rows:
                return rows
            time.sleep(self.retry_sleep * (attempt + 1))
        return []

    def fetch_daily(self, code: str, start: str, end: str | None = None) -> pd.DataFrame:
        merged: dict = {}
        for seg_start, seg_end in self._segments(start, end):
            for r in self._fetch_segment(code, seg_start, seg_end):
                merged[r["date"]] = r
        if not merged:
            return pd.DataFrame(columns=["date", "open", "high", "low", "close",
                                         "volume", "amount"])
        df = pd.DataFrame(merged.values()).sort_values("date").reset_index(drop=True)
        # 负价格是本源的已知缺陷（加法复权所致），落库前拦截，避免污染下游
        if (df["close"] <= 0).any():
            n_bad = int((df["close"] <= 0).sum())
            raise ValueError(
                f"{code} 出现 {n_bad} 条非正价格，复权口径异常（应使用 hfq）")
        return df

    # ---------------- 成分股 ----------------

    def list_constituents(self, index_code: str = "sh000300") -> pd.DataFrame:
        for attempt in range(self.retries):
            text = self._run(["index", "constituent", index_code])
            rows = []
            for line in text.splitlines():
                line = line.strip()
                if not line.startswith("|"):
                    continue
                parts = [p.strip() for p in line.strip("|").split("|")]
                if len(parts) < 2 or parts[0] == "code" or set(parts[0]) <= {"-"}:
                    continue
                rows.append({"code": parts[0], "name": parts[1]})
            if rows:
                return pd.DataFrame(rows).drop_duplicates("code").reset_index(drop=True)
            time.sleep(self.retry_sleep * (attempt + 1))
        raise RuntimeError(f"获取 {index_code} 成分股失败（重试 {self.retries} 次）")

    def index_daily(self, index_code: str, start: str, end: str | None = None) -> pd.DataFrame:
        """指数日线。指数强制不复权，仍走 hfq 参数不影响结果。"""
        return self.fetch_daily(index_code, start, end)


# --------------------------------------------------------------------------
# baostock 数据源（主数据源）
# --------------------------------------------------------------------------

class BaostockSource(BarSource):
    """baostock 数据源 —— 经核验为唯一可信的复权数据源。

    选为主数据源的三条实测依据（见《数据源可用性核验报告》）：
    1. **复权实现正确**。复权因子呈阶梯函数：14 个取值、最长恒定 251 天、
       跳变日精确对应真实除权日（每年6月年度分红 + 12月中期分红）。
       对比 akshare 的东财后复权：2049 个取值、99.6% 交易日都在漂移。
    2. **覆盖退市股票**，可消除生存者偏差。实测：邯郸钢铁（2009退市）2885 条、
       PT金田A（2002退市）2698 条。
    3. **长历史**。茅台可回溯至 2001-08-27，首日收盘 35.55 元与实际一致。

    额外提供 westock 缺失的四项关键字段：
    - `turn` 换手率 → 可反推**事件时点流通市值**（amount/turn），
      解决市值风格分层的前视偏差问题
    - `isST` → 支撑 ST 分层（不需人为剔除即可分开统计）
    - `tradestatus` → 识别停牌，精确处理停牌周
    - `peTTM` / `pbMRQ` → 支撑估值分层

    工程注意：单次查询约 4000 条上限，必须按年分段；session 非线程安全，
    并行须每进程各建一个 login。
    """

    adjustflag: str = "1"        # 1=后复权（唯一可信口径）
    segment: str = "auto"        # auto = 按估算交易日分块，2 段覆盖 2010-至今
    timeout: float = 90.0        # socket 超时（秒）
    name: str = "baostock"

    #: 取数字段。经受控测速（src/bench_fetch.py）：15 列全字段串行单只 17.7s，
    #: 精简至 4 列后降至 11.5s（−35%），16 并发下墙钟 3.0s/只。
    #: 研究只用到 close（收益与均线）与 turn/amount（反推流通市值），
    #: 故只取这四列。`turn` 为百分数，`float_mv = amount*100/turn`。
    FIELDS = "date,close,turn,amount"

    #: 需要保留的全字段集合（如需 isST/tradestatus/peTTM 等做扩展分层时改回）
    FIELDS_FULL = ("date,code,open,high,low,close,preclose,volume,amount,"
                   "turn,tradestatus,pctChg,isST,peTTM,pbMRQ")

    def __init__(self, adjustflag: str = "1", segment: str = "auto",
                 timeout: float = 90.0):
        self.adjustflag = adjustflag
        self.segment = segment
        self.timeout = timeout
        self._logged_in = False

    # ---------------- 代码格式 ----------------

    @staticmethod
    def to_bs(code: str) -> str:
        """westock 风格 sh600519 → baostock 风格 sh.600519。已是目标格式则原样返回。"""
        c = code.strip().lower()
        if "." in c:
            return c
        if c.startswith(("sh", "sz", "bj")) and len(c) > 6:
            return f"{c[:2]}.{c[2:]}"
        return c

    # ---------------- 会话 ----------------

    def _ensure_login(self):
        if not self._logged_in:
            import socket

            import baostock as bs
            # 先打客户端补丁：把"服务端关闭连接导致的无限空转"变成可捕获异常。
            # 否则一旦被限流，worker 会永久卡死，批量建库无法自愈。
            patch_baostock_io(self.timeout)
            # 双保险：让 login 阶段的 TCP connect 也有超时
            socket.setdefaulttimeout(self.timeout)
            lg = bs.login()
            if lg.error_code != "0":
                raise RuntimeError(f"baostock 登录失败: {lg.error_code} {lg.error_msg}")
            self._logged_in = True

    def close(self):
        if self._logged_in:
            import baostock as bs
            bs.logout()
            self._logged_in = False

    def __enter__(self):
        self._ensure_login()
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # ---------------- 日线 ----------------

    def _segments(self, start: str, end: str | None):
        s = pd.Timestamp(start)
        e = pd.Timestamp(end or date.today().isoformat())
        e = min(e, pd.Timestamp(date.today().isoformat()))   # 禁止未来日期
        if s > e:
            return
        if self.segment == "single":
            # 单段直取全区间。实测 2010-至今（4071 交易日）可一次返回完整，
            # 无需分段；比 auto 的 2 段请求省约一半网络时间。
            # 完整性由调用方（build_dataset 的软告警）兜底校验。
            yield s.date().isoformat(), e.date().isoformat()
            return
        if self.segment == "auto":
            # 按「估算交易日数」分块：A股年交易日约 243 天。
            # baostock 单次返回上限约 4000 条，取 3200 留足安全边际，
            # 使 2010-至今只需 2 段（原按年分段需 17 段，耗时 6 倍以上）。
            max_days = 3200
            chunk_days = int(max_days / 0.665)      # 0.665 ≈ 年交易日/年日历日
            cur = s
            while cur <= e:
                seg_end = min(cur + pd.Timedelta(days=chunk_days), e)
                yield cur.date().isoformat(), seg_end.date().isoformat()
                cur = seg_end + pd.Timedelta(days=1)
            return
        cur = s
        while cur <= e:
            if self.segment == "year":
                seg_end = pd.Timestamp(year=cur.year, month=12, day=31)
            elif self.segment == "half_year":
                seg_end = (pd.Timestamp(year=cur.year, month=6, day=30)
                           if cur.month <= 6 else pd.Timestamp(year=cur.year, month=12, day=31))
            else:
                seg_end = e
            seg_end = min(seg_end, e)
            yield cur.date().isoformat(), seg_end.date().isoformat()
            cur = seg_end + pd.Timedelta(days=1)

    def fetch_daily(self, code: str, start: str, end: str | None = None,
                    fields: str | None = None) -> pd.DataFrame:
        import baostock as bs
        self._ensure_login()
        bs_code = self.to_bs(code)
        fields = fields or self.FIELDS

        frames = []
        for s, e in self._segments(start, end):
            rs = bs.query_history_k_data_plus(
                bs_code, fields, start_date=s, end_date=e,
                frequency="d", adjustflag=self.adjustflag)
            if rs.error_code != "0":
                continue
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())
            if rows:
                frames.append(pd.DataFrame(rows, columns=rs.fields))

        if not frames:
            return pd.DataFrame(columns=["date", "open", "high", "low", "close",
                                         "volume", "amount"])

        df = pd.concat(frames, ignore_index=True).drop_duplicates("date")
        df["date"] = pd.to_datetime(df["date"])
        for c in ("open", "high", "low", "close", "preclose", "volume", "amount",
                  "turn", "pctChg", "peTTM", "pbMRQ"):
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")
        for c in ("tradestatus", "isST"):
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).astype(int)

        df = df.dropna(subset=["close"]).sort_values("date").reset_index(drop=True)

        # 事件时点流通市值 = 成交额 / (换手率/100)。turn 为 0（停牌/无成交）时置空。
        if "turn" in df.columns and "amount" in df.columns:
            with np.errstate(divide="ignore", invalid="ignore"):
                mv = np.where(df["turn"] > 0, df["amount"] * 100.0 / df["turn"], np.nan)
            df["float_mv"] = mv

        if (df["close"] <= 0).any():
            bad = int((df["close"] <= 0).sum())
            raise ValueError(f"{code} 出现 {bad} 条非正价格，复权口径异常")
        return df

    # ---------------- 标的清单 ----------------

    def list_universe(self, include_delisted: bool = True) -> pd.DataFrame:
        """全市场标的清单（含退市，这是消除生存者偏差的关键）。

        query_stock_basic 的 status: 1=上市, 0=退市。
        """
        import baostock as bs
        self._ensure_login()
        rs = bs.query_stock_basic()
        rows = []
        while rs.next():
            rows.append(rs.get_row_data())
        df = pd.DataFrame(rows, columns=rs.fields)
        if "type" in df.columns:
            df = df[df["type"] == "1"]           # 仅股票，剔除指数/基金
        if not include_delisted and "status" in df.columns:
            df = df[df["status"] == "1"]
        return df.reset_index(drop=True)

    #: 中证指数官网接口的精确列名。**不可用模糊匹配**——该表同时含
    #: 「指数代码/指数名称/指数英文名称/成分券代码/成分券名称/成分券英文名称」，
    #: 任何宽松匹配都会把多列 rename 成同名，产生重名列导致 df["code"] 变成
    #: DataFrame 而非 Series（v1.1.0 实测报错）。
    _CONS_COLS = {
        "成分券代码": "code",
        "成分券名称": "name",
        "日期": "date",
    }

    def list_constituents(self, index_code: str = "000300") -> pd.DataFrame:
        """指数成分股。baostock 不提供该能力，改用 akshare 的中证指数官网接口。

        注意：该接口返回的是**当前时点**的成分股名单，用于小样本流程验证；
        正式全A口径不使用它（全A样本直接取全市场清单，含退市股）。
        """
        import akshare as ak
        sym = index_code.replace("sh.", "").replace("sh", "").strip()
        df = ak.index_stock_cons_csindex(symbol=sym)
        ren = {k: v for k, v in self._CONS_COLS.items() if k in df.columns}
        df = df.rename(columns=ren)
        if "code" not in df.columns:
            raise RuntimeError(f"{index_code} 成分股接口返回列异常: {list(df.columns)}")
        # 重名列兜底：显式取第一列，避免 df[col] 退化为 DataFrame
        if df.columns.duplicated().any():
            df = df.loc[:, ~df.columns.duplicated()]
        out = pd.DataFrame({
            "code": df["code"].astype(str).str.strip().str.zfill(6).map(self._with_prefix),
            "name": df["name"].astype(str) if "name" in df.columns else "",
        })
        return out.drop_duplicates("code").reset_index(drop=True)

    @staticmethod
    def _with_prefix(code6: str) -> str:
        if code6.startswith(("60", "68", "9")):
            return f"sh.{code6}"
        if code6.startswith(("00", "30", "20")):
            return f"sz.{code6}"
        if code6.startswith(("43", "83", "87", "88")):
            return f"bj.{code6}"
        return code6

    def index_daily(self, index_code: str = "sh.000300", start: str = "2010-01-01",
                    end: str | None = None) -> pd.DataFrame:
        """指数日线。指数不复权，adjustflag 传 3。"""
        import baostock as bs
        self._ensure_login()
        frames = []
        for s, e in self._segments(start, end):
            rs = bs.query_history_k_data_plus(
                index_code, "date,open,high,low,close,volume,amount,pctChg",
                start_date=s, end_date=e, frequency="d", adjustflag="3")
            if rs.error_code != "0":
                continue
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())
            if rows:
                frames.append(pd.DataFrame(rows, columns=rs.fields))
        if not frames:
            return pd.DataFrame(columns=["date", "close"])
        df = pd.concat(frames, ignore_index=True).drop_duplicates("date")
        df["date"] = pd.to_datetime(df["date"])
        for c in ("open", "high", "low", "close", "volume", "amount"):
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")
        return df.dropna(subset=["close"]).sort_values("date").reset_index(drop=True)

    def fetch_industry(self) -> pd.DataFrame:
        """行业分类（证监会行业门类/大类）。用于板块分层。"""
        import baostock as bs
        self._ensure_login()
        rs = bs.query_stock_industry()
        rows = []
        while rs.next():
            rows.append(rs.get_row_data())
        return pd.DataFrame(rows, columns=rs.fields)


# --------------------------------------------------------------------------
# iFinD 数据源（待配置密钥后补全）
# --------------------------------------------------------------------------

class IfindSource(BarSource):
    """iFinD（同花顺）数据源占位实现。

    待用户配置密钥后补全。预期价值：
    - 专业复权实现，可裁定 westock 的复权矛盾
    - **提供退市股票历史数据，消除生存者偏差**（westock 无法提供）
    - 提供历史股本与申万行业分类，支撑市值风格与板块分层

    密钥配置：写入
        C:\\Users\\Administrator\\.workbuddy\\skills\\ifind-finance-data\\mcp_config.json
    调用接口：call-node.js 的 call(server_type, tool_name, params)
    """

    name: str = "ifind"

    def __init__(self, skill_dir: str | None = None, node_exe: str | None = None):
        self.skill_dir = Path(skill_dir or (
            r"C:\Users\Administrator\.workbuddy\skills\ifind-finance-data"))
        self.node_exe = node_exe or (
            r"C:\Users\Administrator\.workbuddy\binaries\node\versions\22.22.2-6\node.exe")

    def is_configured(self) -> bool:
        cfg = self.skill_dir / "mcp_config.json"
        if not cfg.exists():
            return False
        try:
            import json
            token = json.loads(cfg.read_text(encoding="utf-8")).get("auth_token", "")
        except Exception:  # noqa: BLE001
            return False
        return bool(token) and "your ifind" not in token.lower()

    def fetch_daily(self, code: str, start: str, end: str | None = None) -> pd.DataFrame:
        raise NotImplementedError(
            "iFinD 取数待密钥配置后实现。调用 get_stock_performance 获取日频行情，"
            "并需先验证其复权历史深度与批量取数上限。")

    def list_constituents(self, index_code: str) -> pd.DataFrame:
        raise NotImplementedError("iFinD 成分股清单待实现")


# --------------------------------------------------------------------------
# 腾讯行情（兜底源）
# --------------------------------------------------------------------------

class TencentSource(BarSource):
    """腾讯行情兜底源 —— 保证「全市场不遗漏」的最后一道防线。

    引入动机（v1.3.1 事故复盘）：baostock 触发黑名单限流后**返回空值而不报错**，
    且其对部分老股（如 sz.300448 浩云科技、sz.300531 优博讯）本就无数据，
    导致数据仓覆盖面缩水。任何单一数据源都不该是唯一依赖。

    端点::

        https://web.ifzq.gtimg.cn/appstock/app/fqkline/get
            ?param={code6},{period},,,{cnt},{fq}

    实测结论（2026-10-14）：
      * 单次请求上限：日线 640 根、周线 640 根（日/周同值，超出返回 param error）
      * **非窗口锚定**：同一日期在不同 cnt 下取值完全一致 → 可安全增量追加
      * 支持 qfq / hfq / 不复权 三种口径
      * 10 并发抓 104 只周线耗时 < 1 秒，无失败

    ⚠️ 复权口径差异（重要）：
      实测腾讯 hfq 为**仿射复权**（hfq = 2.8×原价 + 常数，含现金分红的加性部分），
      而 baostock hfq 为乘性复权。二者不同源，但对本项目的核心判定
      「周收盘 ≥ 60 周均线」**语义等价**（仿射变换下不等式不变）。
      实测 sh.600020 重叠 138 周信号一致率 98.55%，差异仅出现在
      溢价率 ±0.5pp 的边界周（2025-12-26、2026-10-16）。

      因此本源只作**兜底**：优先 baostock，仅当 baostock 无数据/被限流时启用，
      避免同一只标的的口径在两源之间来回切换。
    """

    name: str = "tencent"

    # 同一接口的多个镜像。腾讯 WAF 会对单主机高并发返回 501 挑战页，
    # 实测 web.ifzq 被限流时 ifzq / proxy.finance 仍然可用 → 必须多主机故障转移。
    HOSTS = (
        "https://ifzq.gtimg.cn/appstock/app/fqkline/get",
        "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/fqkline/get",
        "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
    )
    MAX_BARS = 640            # 实测单次上限（日线/周线同值）
    MIN_INTERVAL = 0.05       # 全局最小请求间隔（秒）≈ 20 req/s，避免触发 WAF
    HOST_COOLDOWN = 300.0     # 某主机被 WAF 拦后的冷却时间（秒）

    _host_lock = threading.Lock()
    _host_down: dict = {}     # host -> 解禁时间戳
    _rate_lock = threading.Lock()
    _next_at = 0.0            # 全局限速（跨线程、跨实例）

    def __init__(self, fq: str = "hfq", timeout: float = 15.0, retries: int = 3,
                 throttle: bool = True):
        self.fq = fq
        self.timeout = timeout
        self.retries = retries
        self.throttle = throttle
        self._sess = None

    # ------------------------------------------------------------------ 内部
    @staticmethod
    def to_tx(code: str) -> str:
        """sh.600000 -> sh600000；已带前缀则原样返回。"""
        c = code.strip().lower()
        if c.startswith(("sh", "sz", "bj")):
            return c.replace(".", "")
        if c.startswith("6"):
            return "sh" + c
        if c.startswith(("0", "3")):
            return "sz" + c
        if c.startswith(("4", "8", "9")):
            return "bj" + c
        return "sz" + c

    def _session(self):
        if self._sess is None:
            import requests
            self._sess = requests.Session()
        return self._sess

    def close(self):
        if self._sess is not None:
            try:
                self._sess.close()
            except Exception:  # noqa: BLE001
                pass
            self._sess = None

    def _throttle(self):
        """全局令牌节流：无论多少线程/实例，请求速率不超过 1/MIN_INTERVAL。"""
        if not self.throttle:
            return
        while True:
            with TencentSource._rate_lock:
                now = time.time()
                if now >= TencentSource._next_at:
                    TencentSource._next_at = now + self.MIN_INTERVAL
                    return
                wait = TencentSource._next_at - now
            time.sleep(min(wait, 0.05))

    @classmethod
    def _hosts_available(cls):
        now = time.time()
        with cls._host_lock:
            alive = [h for h in cls.HOSTS if cls._host_down.get(h, 0) <= now]
        return alive or list(cls.HOSTS)

    @classmethod
    def _mark_host_down(cls, host):
        with cls._host_lock:
            cls._host_down[host] = time.time() + cls.HOST_COOLDOWN

    def _raw(self, code: str, period: str, cnt: int, fq: str | None = None):
        """取原始 K 线数组；period ∈ {day, week, month}。返回 list[list[str]]。"""
        fq = self.fq if fq is None else fq
        code6 = self.to_tx(code)
        path = f"?param={code6},{period},,,{int(cnt)},{fq}"
        last = None
        for attempt in range(self.retries):
            for host in self._hosts_available():
                try:
                    self._throttle()
                    r = self._session().get(
                        host + path, timeout=self.timeout,
                        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"})
                    if r.status_code == 501:
                        # WAF 挑战页（JS 跳转）→ 该主机进入冷却，换下一台
                        self._mark_host_down(host)
                        last = RuntimeError(f"WAF 拦截 {host}")
                        continue
                    j = r.json()
                    if j.get("code") != 0:
                        raise RuntimeError(f"腾讯返回 code={j.get('code')} msg={j.get('msg')}")
                    d = (j.get("data") or {}).get(code6) or {}
                    keys = [k for k in d if period in k]
                    if not keys:
                        return []          # 该标的确实无数据（与"失败"区分）
                    return [row[:6] for row in d[keys[0]]]
                except Exception as exc:  # noqa: BLE001
                    last = exc
            time.sleep(0.5 * (attempt + 1))
        raise RuntimeError(f"腾讯取数失败 {code} {period}: {type(last).__name__}: {last}")

    @staticmethod
    def _to_df(rows) -> pd.DataFrame:
        if not rows:
            return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
        df = pd.DataFrame(rows, columns=["date", "open", "close", "high", "low", "volume"])
        df["date"] = pd.to_datetime(df["date"])
        for c in ("open", "close", "high", "low", "volume"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.dropna(subset=["close"]).drop_duplicates("date").sort_values("date")
        return df.reset_index(drop=True)

    # ------------------------------------------------------------------ 接口
    def fetch_daily(self, code: str, start: str | None = None,
                    end: str | None = None, fq: str | None = None) -> pd.DataFrame:
        """日线。注意：单次上限 640 根，长历史不适用本源。

        fq 可覆盖实例默认值；传 "" 取**不复权**（估算成交额时必须用不复权价）。
        """
        df = self._to_df(self._raw(code, "day", self.MAX_BARS, fq))
        if end:
            df = df[df["date"] <= pd.Timestamp(end)]
        # start 仅作下限裁剪；腾讯不支持任意起点，只能返回最近 MAX_BARS 根
        if start:
            df = df[df["date"] >= pd.Timestamp(start)]
        return df.reset_index(drop=True)

    def fetch_weekly(self, code: str, weeks: int = 320) -> pd.DataFrame:
        """周线（已复权）—— 本项目兜底主用接口。

        返回列 [date, open, high, low, close]。date 为腾讯的周结束日
        （通常为周五，最新未完成周为当日）。
        """
        return self._to_df(self._raw(code, "week", min(weeks, self.MAX_BARS)))

    def list_constituents(self, index_code: str) -> pd.DataFrame:
        raise NotImplementedError("腾讯源不提供成分股清单，请用 baostock/westock")


# --------------------------------------------------------------------------
# 工厂
# --------------------------------------------------------------------------

def get_source(name: str = "westock", **kwargs) -> BarSource:
    if name == "westock":
        return WestockSource(**kwargs)
    if name == "ifind":
        return IfindSource(**kwargs)
    if name == "tencent":
        return TencentSource(**kwargs)
    raise ValueError(f"未知数据源: {name}")


if __name__ == "__main__":
    src = WestockSource()
    print(f"数据源: {src.name}  复权: {src.adjust}  分段: {src.segment}  今日: {src._today}")
    print("\n分段计划（2020-01-01 ~ 2026-10-13）:")
    for s, e in src._segments("2020-01-01", "2026-10-13"):
        print(f"  {s} ~ {e}")
