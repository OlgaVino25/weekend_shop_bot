import sys


START = "/start"
LIST = "/list"

ITEMS = {
    "1": {
        "pk": 1,
        "title": "Кружка",
        "image": "products/cup_1.png",
        "description": "products/cup_1.txt",
    },
    "2": {
        "pk": 2,
        "title": "Худи",
        "image": "products/hoody_m1.jpg",
        "description": "products/hoody_m1.txt",
    },
    "3": {
        "pk": 3,
        "title": "Стикеры",
        "image": "products/sticker_1.jpg",
        "description": "products/sticker_1.txt",
    },
    "4": {
        "pk": 4,
        "title": "Футболка",
        "image": "products/t-short_w1.jpg",
        "description": "products/t-short_w1.txt",
    },
}

ORDER = "/order"


user_message = sys.argv[1]


if user_message == START:
    print("Привет! Я бот-помощник онлайн-магазина")
    print("Кнопка: Список товаров --> /list")

elif user_message == LIST:
    print("Список товаров:")
    for key, item in ITEMS.items():
        print(f"Кнопка: {item['title']} --> {key}")

elif user_message in ITEMS:
    item = ITEMS[user_message]

    print(f"Картинка: {item['image']}")

    with open(item["description"], "r", encoding="utf-8") as f:
        description = f.read()
    print(description)
    print("Кнопка: Заказать --> /order")
    print("Кнопка: Список товаров --> /list")

elif user_message == ORDER:
    print(
        "Ваш заказ принят!\nОжидайте, с Вами свяжется менеджер магазина для уточнения деталей заказа."
    )
    print("Кнопка: Список товаров --> /list")

else:
    print("Ваш вопрос направлен менеджеру магазина.\nОжидайте ответ в этом диалоге.")
    print("Кнопка: Список товаров --> /list")
