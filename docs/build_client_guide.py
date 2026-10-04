"""Build the client-facing Windows setup and operation guide."""

from pathlib import Path
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (
    HRFlowable, Image, PageBreak, Paragraph, SimpleDocTemplate,
    Spacer, Table, TableStyle,
)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "output" / "pdf" / "T-Invest-Bot_Инструкция_клиента.pdf"
SCREEN = ROOT / "docs" / "assets" / "real-order-confirmation-demo.png"
NAVY = colors.HexColor("#16243D")
BLUE = colors.HexColor("#2D5F9D")
GOLD = colors.HexColor("#EAB945")
LIGHT = colors.HexColor("#F2F5FA")
MUTED = colors.HexColor("#53657E")
GREEN = colors.HexColor("#267454")
RED = colors.HexColor("#AF3B3B")

pdfmetrics.registerFont(TTFont("Arial", r"C:\Windows\Fonts\arial.ttf"))
pdfmetrics.registerFont(TTFont("Arial-Bold", r"C:\Windows\Fonts\arialbd.ttf"))
pdfmetrics.registerFontFamily("Arial", normal="Arial", bold="Arial-Bold")

styles = {
    "title": ParagraphStyle("title", fontName="Arial-Bold", fontSize=25, leading=29, textColor=NAVY, spaceAfter=15),
    "deck": ParagraphStyle("deck", fontName="Arial", fontSize=11, leading=16, textColor=MUTED, spaceAfter=12),
    "h1": ParagraphStyle("h1", fontName="Arial-Bold", fontSize=17, leading=21, textColor=NAVY, spaceBefore=10, spaceAfter=9),
    "h2": ParagraphStyle("h2", fontName="Arial-Bold", fontSize=11.5, leading=15, textColor=NAVY, spaceBefore=12, spaceAfter=6),
    "body": ParagraphStyle("body", fontName="Arial", fontSize=9.3, leading=14, textColor=NAVY, spaceAfter=7),
    "small": ParagraphStyle("small", fontName="Arial", fontSize=8.2, leading=12, textColor=MUTED, spaceAfter=5),
    "step": ParagraphStyle("step", fontName="Arial", fontSize=9.3, leading=14, textColor=NAVY, leftIndent=18, firstLineIndent=-18, spaceAfter=9),
    "cell": ParagraphStyle("cell", fontName="Arial", fontSize=8.5, leading=12.5, textColor=NAVY),
    "cell_b": ParagraphStyle("cell_b", fontName="Arial-Bold", fontSize=8.5, leading=12.5, textColor=NAVY),
    "caption": ParagraphStyle("caption", fontName="Arial", fontSize=8, leading=11, textColor=MUTED, alignment=TA_CENTER),
    "overline": ParagraphStyle("overline", fontName="Arial-Bold", fontSize=8.5, leading=12, textColor=BLUE, spaceAfter=8),
}


def P(text, style="body"):
    return Paragraph(text, styles[style])


def step(number, text):
    return P(f'<b>{number}.</b> {text}', "step")


def box(text, *, color=LIGHT, border=BLUE):
    table = Table([[P(text, "body")]], colWidths=[500])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), color),
        ("BOX", (0, 0), (-1, -1), 0.7, border),
        ("LEFTPADDING", (0, 0), (-1, -1), 13),
        ("RIGHTPADDING", (0, 0), (-1, -1), 13),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return table


def grid(rows, widths=(162, 338)):
    data = [[P(a, "cell_b"), P(b, "cell")] for a, b in rows]
    table = Table(data, colWidths=list(widths), hAlign="LEFT")
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, LIGHT]),
        ("LINEBELOW", (0, 0), (-1, -1), 0.35, colors.HexColor("#DCE4EF")),
        ("LEFTPADDING", (0, 0), (-1, -1), 9),
        ("RIGHTPADDING", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    return table


def page(canvas, doc):
    canvas.saveState()
    w, h = A4
    canvas.setFillColor(NAVY)
    canvas.rect(0, h - 13, w, 13, fill=1, stroke=0)
    canvas.setFillColor(GOLD)
    canvas.rect(46, h - 13, 38, 13, fill=1, stroke=0)
    canvas.setStrokeColor(colors.HexColor("#DFE5EE"))
    canvas.line(46, 47, w - 46, 47)
    canvas.setFont("Arial", 7.5)
    canvas.setFillColor(MUTED)
    canvas.drawString(46, 33, "T-Invest Bot  •  Инструкция для клиента  •  04.10.2026")
    canvas.drawRightString(w - 46, 33, str(doc.page))
    canvas.restoreState()


story = []

# Page 1: direct start and operating model.
story += [Spacer(1, 20), P("T-INVEST BOT", "overline"), P("Инструкция для клиента", "title"),
          P("Установка, вход в Пульс, наблюдение за сделками и работа с заявками на вашем компьютере.", "deck"),
          HRFlowable(width="100%", thickness=2, color=GOLD), Spacer(1, 19),
          P("Коротко: как начать", "h1"),
          step(1, "Распакуйте полученный архив в обычную папку на Windows."),
          step(2, "Один раз запустите <b>install.cmd</b> и дождитесь сообщения <b>Setup complete</b>."),
          step(3, "Запустите <b>start_admin.cmd</b>. Оставьте открытым появившееся окно программы."),
          step(4, "Откройте в браузере <b>http://127.0.0.1:8765/</b>, войдите в Пульс и настройте мониторинг."),
          Spacer(1, 6),
          box("<b>Главное правило:</b> программа работает локально, пока запущено окно <b>start_admin.cmd</b> и компьютер включён. Вкладку с панелью можно закрыть и открыть снова. Если закрыть окно программы, выключить компьютер или перевести его в сон, новые сделки перестанут проверяться."),
          Spacer(1, 15), P("Что нужно заранее", "h2"),
          grid([
              ("Компьютер", "Windows, Python 3.10 или новее, Brave / Edge / Chrome, доступ в интернет."),
              ("Пульс", "Ваш обычный вход в Т-Банк. Он нужен, чтобы читать опубликованные сделки выбранного автора."),
              ("Telegram", "По желанию: токен своего бота и числовой ID получателя. Без ID сообщений не будет."),
              ("Брокерский счёт", "Для просмотра счёта - API-токен только для чтения. Для настоящих заявок - отдельный токен с полным доступом."),
          ]),
          Spacer(1, 11), P("Панель доступна только на этом компьютере по адресу 127.0.0.1. Это не облачный сервис. Браузер обращается к Пульсу, программа - к T-Invest API и Telegram, если эти функции включены.", "small"),
          PageBreak()]

# Page 2: installation and login.
story += [P("1. Установка и первый запуск", "h1"),
          step(1, "Распакуйте архив полностью, например в папку <b>Документы\\T-Invest-Bot</b>. Не запускайте файлы прямо из ZIP-архива."),
          step(2, "Убедитесь, что установлен Python 3.10+ с официального сайта <link href='https://www.python.org/downloads/windows/' color='#2D5F9D'>python.org/downloads/windows</link>. Для работы с Пульсом понадобится Brave, Edge или Chrome."),
          step(3, "Дважды нажмите <b>install.cmd</b>. При первом запуске программа создаст папку <b>.venv</b> и установит зависимости. Для этого нужен интернет. Дождитесь <b>Setup complete</b>, затем закройте установщик."),
          step(4, "Дважды нажмите <b>start_admin.cmd</b>. В окне программы появится адрес <b>http://127.0.0.1:8765/</b>. Не закрывайте это окно во время работы."),
          step(5, "Откройте адрес в браузере. Если появилось сообщение, что порт занят, проверьте: возможно, другая копия программы уже запущена."),
          P("2. Подключение Пульса", "h1"),
          step(1, "В панели нажмите <b>«Подключить Пульс»</b> или <b>«Открыть окно Пульса»</b>. Откроется отдельное окно Т-Банка."),
          step(2, "Войдите в свой аккаунт Т-Банка в этом окне. После входа в панели нажмите <b>«Проверить вход»</b>. Программа откроет сделки выбранного автора в том же окне."),
          step(3, "Дождитесь, пока на «Обзоре» появятся инструменты и сделки. Окно банка во время подключения не закрывайте; после успешного входа его можно свернуть."),
          box("<b>Вход сохраняется локально.</b> При следующем запуске программа попробует восстановить сеанс. Если банк завершил его, откройте окно Пульса и войдите снова. Пароль и SMS-код в админку вводить не нужно."),
          Spacer(1, 11), P("Первое чтение профиля задаёт исходную точку: старые сделки автоматически не повторяются. При желании опубликованную покупку за последние 24 часа можно обработать вручную на «Обзоре».", "small"),
          PageBreak()]

# Page 3: monitoring and paper mode.
story += [P("3. Обзор и мониторинг", "h1"),
          P("На вкладке <b>«Обзор»</b> видны профиль автора, сделки за 24 часа, список инструментов, условный результат, статус Telegram и количество реальных заявок. Нажмите на инструмент, чтобы посмотреть его историю. Вкладка <b>«Журнал»</b> показывает сигналы, действия и причины ошибок."),
          P("Кнопка <b>«Посмотреть за месяц»</b> в блоке сделок загружает историю за последние 30 дней. Загрузка может занять несколько минут; панель показывает ход проверки. Сделки старше 24 часов доступны только для просмотра.", "small"),
          P("Настройки мониторинга", "h2"),
          step(1, "Откройте <b>«Настройки»</b>. Вставьте ссылку на профиль автора Пульса; изначально указан LinMath."),
          step(2, "Включите переключатель <b>«Мониторинг сделок Пульса»</b>, задайте интервал от 30 до 3600 секунд и нажмите <b>«Сохранить настройки»</b>."),
          step(3, "Проверьте на «Обзоре» статус «Мониторинг включён». Новые операции появятся в «Журнале» после публикации и очередной проверки."),
          box("<b>Ограничение источника:</b> Пульс показывает публичные операции автора без их объёма. Программа вычисляет объём своих сделок по вашим лимитам. Задержка публикации Пульса и стабильность его внутреннего интерфейса не гарантированы."),
          P("Проверка без реальных денег", "h2"),
          P("Оставьте поле <b>«Реальная торговля» → «Выключена»</b>. На «Обзоре» кнопка <b>«Повторить покупку»</b> у сделки автора за последние 24 часа создаст только условную покупку. Кнопка <b>«Включить автокопирование»</b> будет условно обрабатывать новые покупки и продажи. Результат появится в «Нашем результате» и «Журнале», брокеру заявка не отправится."),
          P("Лимиты", "h2"),
          grid([
              ("Акции, облигации, фонды", "Бюджет покупки, максимальная цена одного лота и лимит всей позиции."),
              ("Фьючерсы", "Лимит гарантийного обеспечения и максимальное число контрактов."),
              ("Продажа", "Процент собственной позиции, которую разрешено закрыть. Продажа без доступной позиции бота блокируется."),
          ]),
          Spacer(1, 10),
          P("Перед включением реальной торговли проверьте все суммы. Значения в установленной панели - стартовые настройки, а не подтверждённые лимиты для вашего счёта.", "small"),
          PageBreak()]

# Page 4: notifications and API account.
story += [P("4. Telegram и брокерский счёт", "h1"),
          P("Уведомления в Telegram", "h2"),
          step(1, "Создайте своего бота через <b>@BotFather</b> в Telegram и сохраните его токен. Откройте чат с ботом и нажмите <b>Start</b>."),
          step(2, "Узнайте числовой ID своего личного чата. Для этого можно прочитать поле <b>message.chat.id</b> в ответе метода Telegram <b>getUpdates</b> после сообщения боту; это должен быть ID получателя, а не имя бота."),
          step(3, "На вкладке «Настройки» вставьте токен бота и <b>Telegram ID получателя</b>, нажмите «Сохранить настройки». На «Обзоре» в блоке Telegram нажмите <b>«Проверить отправку»</b>."),
          box("Если Telegram ID пустой, программа <b>ничего не отправляет</b>, даже если токен бота сохранён. «Пауза уведомлений» останавливает обычные сообщения; важные ошибки реальных заявок всё равно могут отправляться. Для остановки торговли выключайте именно режим реальной торговли."),
          P("Просмотр своего брокерского счёта", "h2"),
          step(1, "В личном кабинете Т-Инвестиций выпустите API-токен <b>только для чтения</b> с доступом к нужному счёту. Официальная инструкция: <link href='https://developer.tbank.ru/invest/intro/intro/token' color='#2D5F9D'>developer.tbank.ru/invest/intro/intro/token</link>."),
          step(2, "В «Настройках» → <b>«Брокерский счёт»</b> вставьте токен, нажмите «Подключить» и выберите счёт. Кнопка «Обновить счёт» перечитает рубли и позиции."),
          P("Подключение только для чтения не включает покупки. Вход в Пульс, Telegram-бот и API-токен брокерского счёта - отдельные подключения.", "small"),
          P("Где хранятся настройки", "h2"),
          P("Настройки, журналы, состояние наблюдения, вход Пульса и токены находятся в локальной папке <b>.local</b> рядом с программой. Не пересылайте эту папку, не кладите её в общий архив и не удаляйте после начала реальной торговли: в ней хранится учёт уже обработанных сделок и заявок.", "body"),
          PageBreak()]

# Page 5: live orders and screenshot.
story += [P("5. Реальные покупки и продажи", "h1"),
          P("По умолчанию реальная торговля выключена. Для неё нужен <b>отдельный API-токен с полным доступом</b> к открытому брокерскому счёту. Создайте его в Т-Инвестициях, вставьте в «Настройки» → «Реальные заявки», нажмите <b>«Проверить токен»</b> и выберите счёт. Никому не отправляйте токен сообщением."),
          grid([
              ("Выключена", "Новых брокерских заявок нет; доступны мониторинг и симуляция."),
              ("Подтверждать каждую заявку", "Новый сигнал ожидает решения в «Журнале». Перед отправкой проверяются свежая цена, деньги, лоты и лимиты; затем открывается окно подтверждения."),
              ("Автоматически по лимитам", "Новые сигналы после включения проверяются и могут отправляться брокеру без нажатия кнопки. Прошлые сделки автоматически не покупаются."),
          ]),
          Spacer(1, 9),
          P("Включение: проверьте лимиты и Telegram, включите мониторинг, выберите режим в поле <b>«Реальная торговля»</b> и нажмите <b>«Сохранить настройки»</b>. Для первых проверок используйте режим подтверждения и небольшие суммы. Настоящая заявка с пользовательским токеном ещё не проходила проверку разработчиком на реальном счёте.", "body"),
          P("Как выглядит подтверждение", "h2"),
          Image(str(SCREEN), width=390, height=390 * ImageReader(str(SCREEN)).getSize()[1] / ImageReader(str(SCREEN)).getSize()[0]),
          P("Иллюстрация интерфейса с вымышленным счётом и тестовыми суммами. Кнопка «Отправить реальную заявку» действительно отправляет поручение брокеру.", "caption"),
          PageBreak()]

# Page 6: failure modes and stop.
story += [P("6. Ошибки, остановка и повторный запуск", "h1"),
          grid([
              ("Нет денег или позиции", "Заявка не отправляется. Причина записывается в журнал; при настроенном ID приходит сообщение в Telegram."),
              ("Брокер отклонил заявку", "Смотрите статус и причину в журнале, проверьте счёт и открытые заявки в приложении Т-Банка."),
              ("Нет ответа сети", "Статус заявки может быть неопределённым. Программа не отправляет ту же сделку повторно вслепую; сверяет её по ID с брокером."),
              ("Пульс перестал отвечать", "Проверьте вход в отдельном окне Т-Банка и нажмите «Подключить Пульс» / «Проверить вход»."),
              ("Нет Telegram-сообщения", "Проверьте числовой ID, токен, что вы нажали Start в чате с ботом, и кнопку «Проверить отправку»."),
          ]),
          P("Как остановить работу", "h2"),
          step(1, "Для остановки <b>новых реальных заявок</b> в «Настройках» выберите <b>«Реальная торговля» → «Выключена»</b> и сохраните."),
          step(2, "Чтобы полностью остановить мониторинг, закройте окно программы <b>start_admin.cmd</b> или нажмите в нём <b>Ctrl+C</b>. Закрытие вкладки браузера не останавливает программу."),
          step(3, "Откройте приложение Т-Банка и проверьте уже выставленные лимитные заявки. Выключение режима и закрытие программы <b>не отменяют</b> их автоматически."),
          P("После перезапуска", "h2"),
          P("Снова запустите <b>start_admin.cmd</b> и откройте <b>http://127.0.0.1:8765/</b>. Настройки и журналы останутся в папке <b>.local</b>. Если банк завершил вход, авторизуйтесь в Пульсе снова. Программа работает с одним выбранным профилем и не выполняет проверки по расписанию, когда выключена.", "body"),
          box("<b>Если установка не прошла:</b> убедитесь, что архив распакован, Python 3.10+ установлен и есть интернет. Повторно запустите install.cmd и сохраните текст ошибки из окна - он поможет при поддержке."),
          Spacer(1, 12),
          P("Официальные справки: <link href='https://developer.tbank.ru/invest/intro/intro/token' color='#2D5F9D'>токены T-Invest API</link> · <link href='https://core.telegram.org/bots/features#creating-a-new-bot' color='#2D5F9D'>создание Telegram-бота</link> · <link href='https://www.python.org/downloads/windows/' color='#2D5F9D'>Python для Windows</link>.", "small")]


def main():
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(str(OUTPUT), pagesize=A4, rightMargin=46, leftMargin=46,
                            topMargin=46, bottomMargin=62, title="T-Invest Bot — инструкция для клиента",
                            author="T-Invest Bot")
    doc.build(story, onFirstPage=page, onLaterPages=page)
    print(OUTPUT)


if __name__ == "__main__":
    main()
