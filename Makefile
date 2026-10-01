.PHONY: install backend frontend preflight preflight-fresh preflight-list

install:
	cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
	cd frontend && npm install

backend:
	cd backend && ./run.sh

frontend:
	cd frontend && npm run dev

# 公众诉求发布前检查：步骤清单只维护在 release-check/manifest.json，
# 这里只负责调用流程。默认断点续跑；失败修好后重跑本目标会从失败步骤继续。
preflight:
	python3 release-check/run_checks.py

# 清掉上一次的全部中间产物与戳，从头检查
preflight-fresh:
	python3 release-check/run_checks.py --fresh

# 查看当前清单里有哪些检查步骤
preflight-list:
	python3 release-check/run_checks.py --list
