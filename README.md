Пакеты системы (Debian/Ubuntu):

sudo apt update  
sudo apt install python3 python3-venv python3-pip  
На Fedora: python3 и python3-pip. Git не обязателен.  
  
Проект  
  
cd /path/to/wb-price-tracker  
python3 -m venv .venv  
.venv/bin/python -m pip install -r requirements.txt  
cp .env.example .env  
В .env — токен и chat id. В config.json — товары WB.  
  
Запуск  
  
.venv/bin/python tracker.py  
.venv/bin/python tracker.py --watch  
По расписанию — cron или systemd timer на этот же .venv/bin/python.  

