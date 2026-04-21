# CTP (China Futures) Documentation

## English

Welcome to the CTP (China Futures) documentation for bt_api.

### Quick Start

```bash
pip install bt_api_ctp
```

```python
from bt_api_ctp import CTPApi
feed = CTPApi(api_key="your_key", secret="your_secret")
ticker = feed.get_ticker("rb2401")
```

## 中文

欢迎使用 bt_api 的 CTP期货 文档。

### 快速开始

```bash
pip install bt_api_ctp
```

```python
from bt_api_ctp import CTPApi
feed = CTPApi(api_key="your_key", secret="your_secret")
ticker = feed.get_ticker("rb2401")
```

## API Reference

See source code in `src/bt_api_ctp/` for detailed API documentation.
