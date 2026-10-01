.PHONY: install backend frontend preflight preflight-reset preflight-status

install:
	cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
	cd frontend && npm install

backend:
	cd backend && ./run.sh

frontend:
	cd frontend && npm run dev

# 公众诉求发布前检查：以 release-check/manifest.toml 为唯一清单跑完整条流程
preflight:
	python3 release-check/preflight.py

# 丢弃上次进度与中间产物，从头再跑
preflight-reset:
	python3 release-check/preflight.py --reset

# 只看各检查步骤当前通过情况
preflight-status:
	python3 release-check/preflight.py --status
