import datetime
import sys
import os

def main():
    today = datetime.date.today()
    target = datetime.date(2027, 1, 1)
    delta = target - today
    days_left = delta.days

    if days_left < 0:
        message = f"The date January 1, 2027 was {abs(days_left)} days ago."
    elif days_left == 0:
        message = "Today is January 1, 2027!"
    else:
        message = f"There are {days_left} days left until January 1, 2027."

    try:
        with open("countdown.txt", "w", encoding="utf-8") as f:
            f.write(message + "\n")
    except OSError as e:
        sys.stderr.write(f"Error writing to countdown.txt: {e}\n")
        sys.exit(1)

if __name__ == "__main__":
    main()