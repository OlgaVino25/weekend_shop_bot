import argparse
import asyncio
import configparser
import json
import logging
import mimetypes
import re
import sys
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from typing import AsyncGenerator, Annotated, Collection, Any, Callable
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

CONFIG_FILE_PATH = Path(__file__).parent / Path('config.ini')
CGI_SCRIPT_FILE_PATH = Path(__file__).parent / Path('bot.py')

logger = logging.getLogger(__name__)


def get_python_binary_abs_path():
    abs_path = sys.executable
    if abs_path.lower().endswith('w.exe'):
        # On Windows, use python.exe, not pythonw.exe
        abs_path = abs_path[:-5] + abs_path[-4:]
    return abs_path


PYTHON_BINARY_ABS_PATH = get_python_binary_abs_path()
DEFAULT_TERMINATION_DELAY = 2


class SubprocessExited(Exception):
    """Exception reports about subprocess was exited."""

    def __init__(self, subprocess_exit_code):
        self.subprocess_exit_code = subprocess_exit_code


async def read_text_lines(stream: asyncio.StreamReader) -> AsyncGenerator[str, None]:
    while not stream.at_eof():
        text_line_bytes = await stream.readline()

        # Skip trailing empty line -- it happens when stream is already closed.
        if not text_line_bytes:
            continue

        text_line = text_line_bytes.decode()  # FIXME Правильно ли выбрана кодировка?

        # remove trailing symbols /t and other whitespaces if occur
        text_line = text_line.rstrip()

        yield text_line


@asynccontextmanager
async def run_py_script(
        py_script_path: str | Path,
        *,
        args: Collection[str],
        script_working_directory: Annotated[
            str | Path,
            "Working directory will be set for subprocess with py-script",
        ] = Path.cwd(),
        termination_delay: Annotated[
            int,
            "Delay being waited till py-script ends it work before hard kill send with SIGKILL.",
        ] = DEFAULT_TERMINATION_DELAY
) -> AsyncGenerator[AsyncGenerator[str, None], None]:
    """
    Run a subprocess for py-script with path specified, yield subprocess std output text lines.

    Returns subprocess return code. Value `0` means success. Other values are error codes.

    Error messages will be printed to console directly.

    Subprocess will be terminated gracefully if coroutine cancellation occurs.
    """
    process = await asyncio.create_subprocess_exec(
        *[
            str(PYTHON_BINARY_ABS_PATH),
            '-u',  # Force the stdout and stderr streams to be unbuffered.
            str(py_script_path),
            *args,
        ],
        cwd=script_working_directory,
        stdout=asyncio.subprocess.PIPE,
        stderr=None,  # will make the subprocess inherit the file descriptor from current process
    )

    try:
        yield read_text_lines(process.stdout)  # yield a whole async generator, not a single value
    finally:
        # FIXME Should protect code with asyncio.shield ?
        if process.returncode is not None:
            raise SubprocessExited(process.returncode)

        process.terminate()  # send Terminate signal to subprocess

        with suppress(asyncio.TimeoutError):
            result = await asyncio.wait_for(process.communicate(), timeout=termination_delay)

        # FIXME Считать всё что успело накопиться в stdout и stderr

        if process.returncode is None:
            process.kill()  # send KILL signal to subprocess

        await process.communicate()

        while not process.stdout.at_eof():
            text_line_bytes = await process.stdout.readline()
            text_line = text_line_bytes.decode(sys.getdefaultencoding())  # FIXME Правильно ли выбрана кодировка?

            # remove trailing symbols /t and other whitespaces if occur
            text_line = text_line.rstrip()

            yield text_line

        raise SubprocessExited(process.returncode)


@dataclass
class TelegramBot:
    token: str
    url: str = 'https://api.telegram.org/bot'
    messages: dict[int, int] = field(default_factory=lambda: defaultdict(int))

    @property
    def bot_url(self) -> str:
        return f'{self.url}{self.token}/'

    @staticmethod
    async def _run_sync_in_executor(
            method: Callable,
            *args: object
    ) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, method, *args)
        if "error" in result:
            logger.error("Ошибка %s: ", result['error'])
        return result

    def _send_request(
            self,
            telegram_method: str,
            params: dict[str, Any] | None = None,
            body: bytes | None = None,
            content_type: str | None = None,
            method: str = "GET"
    ) -> dict[str, Any]:
        url = f"{self.bot_url}{telegram_method}"
        if params:
            url = f'{url}?{urlencode(params)}'

        req = Request(url, data=body)
        if content_type:
            req.add_header('Content-Type', content_type)
        req.method = method

        try:
            with urlopen(req) as response:
                if response.status != HTTPStatus.OK:
                    return {"error": f"HTTP error {response.status}"}
                resp_data = response.read().decode()
                response = json.loads(resp_data)
                logger.debug("request result %s: ", response)

                # фиксируем message_id у которого потом надо убрать кнопки
                result = response.get('result')
                if response.get('ok') and isinstance(result, dict):
                    chat_id = result['chat']['id']
                    if 'message_id' in result and 'reply_markup' in result:
                        self.messages[chat_id] = result['message_id']

                return response
        except Exception as e:
            return {"error": str(e)}

    @staticmethod
    def _encode_multipart_formdata(fields: dict[str, Any], files: list[tuple[str, Path, bytes]]) -> tuple[str, bytes]:
        boundary = uuid.uuid4().hex
        boundary_bytes = boundary.encode('utf-8')
        crlf = b'\r\n'
        body = b''

        # Поля формы
        for (key, value) in fields.items():
            body += b'--' + boundary_bytes + crlf
            body += f'Content-Disposition: form-data; name="{key}"'.encode('utf-8') + crlf + crlf
            body += value.encode('utf-8') + crlf

        # Файлы
        for (key, filename, filecontent) in files:
            body += b'--' + boundary_bytes + crlf
            mimetype = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
            body += f'Content-Disposition: form-data; name="{key}"; filename="{filename}"'.encode('utf-8') + crlf
            body += f'Content-Type: {mimetype}'.encode('utf-8') + crlf + crlf
            body += filecontent + crlf

        body += b'--' + boundary_bytes + b'--' + crlf
        content_type = f'multipart/form-data; boundary={boundary}'
        return content_type, body

    def send_message_sync(
            self,
            chat_id: int | str,
            message: str,
            keyboard: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        method = 'sendMessage'
        params = {
            "chat_id": chat_id,
            "text": message,
        }
        if keyboard:
            params["reply_markup"] = json.dumps({"inline_keyboard": [keyboard]})

        self.del_buttons_sync(chat_id)

        return self._send_request(method, params)

    async def send_message(
            self,
            chat_id: int | str,
            message: str,
            keyboard: list[list[dict[str, Any]]] | None = None
    ) -> dict[str, Any]:
        return await self._run_sync_in_executor(self.send_message_sync, chat_id, message, keyboard)

    def edit_message_reply_markup_sync(
            self,
            chat_id: int | str,
            message_id: int,
            keyboard: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        method = 'editMessageReplyMarkup'
        params = {
            "chat_id": chat_id,
            "message_id": message_id,
        }
        if keyboard:
            params["reply_markup"] = json.dumps({"inline_keyboard": [keyboard]})

        return self._send_request(method, params)

    async def edit_message_reply_markup(
            self,
            chat_id: int | str,
            message_id: int,
            keyboard: list[list[dict[str, Any]]] | None = None
    ) -> dict[str, Any]:
        return await self._run_sync_in_executor(self.edit_message_reply_markup_sync, chat_id, message_id, keyboard)

    def get_updates_sync(
            self,
            offset: int | None = None,
            timeout: int = 30
    ) -> dict[str, Any]:
        method = 'getUpdates'
        params = {
            "timeout": timeout,
            "allowed_updates": json.dumps(["message", "callback_query"]),
        }
        if offset:
            params["offset"] = offset

        return self._send_request(method, params)

    async def send_photo(
            self, chat_id: int | str,
            photo: str | Path,
            caption: str | None = None
    ) -> dict[str, Any] | None:
        # Проверим, является ли photo локальным файлом
        path = Path(photo)
        if path.is_file():
            # Отправляем как файл
            return await self.send_photo_by_path(chat_id, photo, caption)
        else:
            # Проверим, что это ссылка (url)
            url = str(photo)
            parsed = urlparse(url)
            if parsed.scheme in ("http", "https") and parsed.netloc:
                # Отправляем как ссылку
                return await self.send_photo_by_url(chat_id, url, caption)

        logger.error('Неверный путь к фотографии или неверный формат %s', photo)
        return None

    def send_photo_by_url_sync(self, chat_id: int | str, photo_url: str, caption: str | None = None) -> dict[str, Any]:
        method = 'sendPhoto'
        params = {
            "chat_id": chat_id,
            "photo": photo_url,
        }
        if caption:
            params["caption"] = caption

        self.del_buttons_sync(chat_id)

        return self._send_request(method, params=params)

    async def send_photo_by_url(self, chat_id: int | str, photo_url: str, caption: str | None = None) -> dict[str, Any]:
        return await self._run_sync_in_executor(self.send_photo_by_url_sync, chat_id, photo_url, caption)

    def send_photo_by_path_sync(
            self,
            chat_id: int | str,
            photo_path: Path,
            caption: str | None = None
    ) -> dict[str, Any]:
        method = 'sendPhoto'

        # Читаем файл картинки
        with open(photo_path, 'rb') as f:
            file_content = f.read()

        fields = {
            'chat_id': str(chat_id),
        }
        if caption:
            fields['caption'] = caption

        files = [
            ('photo', photo_path, file_content),
        ]

        content_type, body = self._encode_multipart_formdata(fields, files)

        self.del_buttons_sync(chat_id)

        return self._send_request(method, body=body, content_type=content_type, method="POST")

    def del_buttons_sync(self, chat_id):
        if message_id := self.messages.get(chat_id):
            logger.debug('удаляем кнопки у %s', message_id)
            edited = self.edit_message_reply_markup_sync(chat_id=chat_id, message_id=message_id)
            if edited.get('ok'):
                self.messages.pop(chat_id)

    async def send_photo_by_path(
            self,
            chat_id: int | str,
            photo_path: str | Path,
            caption: str | None = None
    ) -> dict[str, Any]:
        return await self._run_sync_in_executor(self.send_photo_by_path_sync, chat_id, photo_path, caption)

    async def run_long_polling(self, timeout: int = 30) -> AsyncGenerator[dict[str, Any], None]:
        offset = None
        while True:
            updates = await self._run_sync_in_executor(self.get_updates_sync, offset, timeout)
            if updates is None:
                logger.error("Не удалось получить обновления, повтор запроса через 5 секунд")
                await asyncio.sleep(5)
                continue

            for update in updates.get("result", []):
                offset = update["update_id"] + 1
                yield update

            await asyncio.sleep(1)


async def process_script(stdout: AsyncGenerator[str, None], bot: TelegramBot) -> list[tuple[Callable, Any]]:
    messages = []
    text_lines = []
    buttons = []
    keyboard: list[dict[str, Any]] = []
    async for text_line in stdout:
        if text_line == '---':
            continue

        keyboard = []
        if text_line.startswith('Кнопка:'):
            buttons.append(text_line)
            continue

        if text_line.startswith('Картинка:'):
            photo_path = text_line.split("Картинка:")[1].strip()
            messages.append((bot.send_photo, photo_path))
            text_line = ''

        if text_line:
            logger.info('Бот говорит: %s', text_line)
            text_lines.append(text_line)

    if buttons:
        keyboard = get_keyboard(','.join(buttons))

    if text_lines:
        message = '\n'.join(text_lines)
        messages.append((bot.send_message, message, keyboard))

    return messages


def get_keyboard(text_line: str) -> list[dict[str, Any]]:
    pattern = r'Кнопка:\s*([^,]+?)\s*-->\s*([^\s,]+)'
    matches = re.findall(pattern, text_line)

    result = [{'text': text, 'callback_data': callback} for text, callback in matches]
    return result


async def main():
    """Main function is just an example of run_py_script function usage."""
    config = configparser.ConfigParser()
    config.read(CONFIG_FILE_PATH)

    parser = argparse.ArgumentParser(description="Запуск CGI сервера для телеграм бота")
    parser.add_argument(
        "-bot_token",
        type=str,
        help="Токен Telegram бота",
        default=config['Telegram']['token'],
    )
    parser.add_argument(
        "-script_path",
        type=Path,
        default=CGI_SCRIPT_FILE_PATH,
        help="Путь к cgi скрипту"
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="уровень логирования (default: INFO)"
    )

    cmd_args = parser.parse_args()
    logging.basicConfig(
        level=logging.getLevelName(cmd_args.log_level.upper()),
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[logging.StreamHandler(sys.stdout), ]
    )

    bot = TelegramBot(cmd_args.bot_token)

    async for update in bot.run_long_polling():
        logger.debug('update %s', update)

        # определяем text и chat_id
        match update:
            case {"message": {"text": text, "chat": {"id": chat_id}, "message_id": message_id}}:
                pass
            case {"callback_query": {"data": text, "message": {"chat": {"id": chat_id}, "message_id": message_id}}}:
                pass
            case _:
                continue

        logger.info("Получено обновление от Telegramm: message_id - %s, chat_id - %s", message_id, chat_id)

        if not text:
            continue
        logger.info('Юзер говорит: %s', text)

        try:
            async with run_py_script(cmd_args.script_path, args=[text]) as stdout:
                messages_to_send = await process_script(stdout, bot)

        except SubprocessExited as error:
            if error.subprocess_exit_code != 0:
                logger.error('Скрипт завершил работу с кодом ошибки %s', error.subprocess_exit_code)

        for method_to_call, *args in messages_to_send:
            await method_to_call(chat_id, *args)


if __name__ == '__main__':

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info('\nСервер остановлен пользователем.')
