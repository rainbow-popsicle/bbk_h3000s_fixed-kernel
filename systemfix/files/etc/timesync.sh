#!/system/bin/sh
# 开机校时：NTP(UDP 123) 被运营商封锁，改用 HTTP 响应头的 Date
i=0
while [ $i -lt 20 ]; do
  HDR=$(curl -sI --max-time 8 https://www.baidu.com 2>/dev/null | grep -i '^date:')
  if [ -n "$HDR" ]; then
    T=$(echo "$HDR" | sed -E 's/.*, ([0-9]{2}) ([A-Za-z]{3}) ([0-9]{4}) ([0-9]{2}):([0-9]{2}):([0-9]{2}).*/\1 \2 \3 \4 \5 \6/')
    D=$(echo $T | cut -d' ' -f1); M=$(echo $T | cut -d' ' -f2); Y=$(echo $T | cut -d' ' -f3)
    H=$(echo $T | cut -d' ' -f4); MI=$(echo $T | cut -d' ' -f5); S=$(echo $T | cut -d' ' -f6)
    case $M in
      Jan) MN=01;; Feb) MN=02;; Mar) MN=03;; Apr) MN=04;; May) MN=05;; Jun) MN=06;;
      Jul) MN=07;; Aug) MN=08;; Sep) MN=09;; Oct) MN=10;; Nov) MN=11;; Dec) MN=12;; *) MN=01;;
    esac
    date -u $MN$D$H$MI$Y.$S
    echo "timesync ok: $(date)" >> /data/local/tmp/timesync.log
    exit 0
  fi
  i=$((i+1))
  sleep 10
done
echo "timesync failed" >> /data/local/tmp/timesync.log