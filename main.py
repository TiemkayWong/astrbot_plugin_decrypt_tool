import math
import time
from io import BytesIO
from PIL import Image
import numpy as np
import aiohttp
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
import astrbot.api.message_components as Comp
from .core.forward_manager import ForwardManager

@register("decrypt_tool", "TiemkayWong", "用于解析番茄混淆网站图片的工具", "0.1.0")
class TomatoImageDecryptor(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    async def initialize(self):
        logger.info("番茄图片解混淆插件已加载")

    def _gilbert2d(self, width, height):
        coordinates = []
        if width >= height:
            self._generate2d(0, 0, width, 0, 0, height, coordinates)
        else:
            self._generate2d(0, 0, 0, height, width, 0, coordinates)
        return coordinates

    def _generate2d(self, x, y, ax, ay, bx, by, coordinates):
        w = abs(ax + ay)
        h = abs(bx + by)
        dax = 1 if ax > 0 else -1 if ax < 0 else 0
        day = 1 if ay > 0 else -1 if ay < 0 else 0
        dbx = 1 if bx > 0 else -1 if bx < 0 else 0
        dby = 1 if by > 0 else -1 if by < 0 else 0
        if h == 1:
            for i in range(w):
                coordinates.append([x, y])
                x += dax
                y += day
            return
        if w == 1:
            for i in range(h):
                coordinates.append([x, y])
                x += dbx
                y += dby
            return
        ax2 = ax // 2
        ay2 = ay // 2
        bx2 = bx // 2
        by2 = by // 2
        w2 = abs(ax2 + ay2)
        h2 = abs(bx2 + by2)
        if 2 * w > 3 * h:
            if (w2 % 2) and (w > 2):
                ax2 += dax
                ay2 += day
            self._generate2d(x, y, ax2, ay2, bx, by, coordinates)
            self._generate2d(x + ax2, y + ay2, ax - ax2, ay - ay2, bx, by, coordinates)
        else:
            if (h2 % 2) and (h > 2):
                bx2 += dbx
                by2 += dby
            self._generate2d(x, y, bx2, by2, ax2, ay2, coordinates)
            self._generate2d(x + bx2, y + by2, ax, ay, bx - bx2, by - by2, coordinates)
            self._generate2d(x + (ax - dax) + (bx2 - dbx), y + (ay - day) + (by2 - dby),
                             -bx2, -by2, -(ax - ax2), -(ay - ay2), coordinates)

    async def _decrypt_image(self, image_data: bytes) -> bytes:
        try:
            img = Image.open(BytesIO(image_data)).convert('RGB')
            width, height = img.size
            pixels = np.array(img)
            curve = self._gilbert2d(width, height)
            offset = round((math.sqrt(5) - 1) / 2 * width * height)
            new_pixels = np.zeros_like(pixels)
            for i in range(width * height):
                old_pos = curve[i]
                new_pos = curve[(i + offset) % (width * height)]
                new_pixels[old_pos[1], old_pos[0]] = pixels[new_pos[1], new_pos[0]]
            output = BytesIO()
            Image.fromarray(new_pixels).save(output, format='JPEG', quality=95)
            return output.getvalue()
        except Exception as e:
            logger.error(f"图片解混淆失败: {str(e)}")
            raise

    @filter.command("解混淆", alias={"解密图片", "deconfuse"})
    async def decrypt_command(self, event: AstrMessageEvent):
        event.should_call_llm(False)
        try:
            if event.message_obj.group_id:
                logger.info("群聊消息，跳过解混淆处理")
                event.stop_event()
                return
            
            messages = event.message_obj.message
            #  获取所有图片
            image_segments = [seg for seg in messages if isinstance(seg, Comp.Image)]
            if not image_segments:
                reply_seg = next((seg for seg in messages if isinstance(seg, Comp.Reply)), None)
                if reply_seg and reply_seg.chain:
                    image_segments = [seg for seg in reply_seg.chain if isinstance(seg, Comp.Image)]
            if not image_segments:
                yield event.plain_result("请发送带有混淆图片的消息或回复一条带有图片的消息")
                return
            
            #  处理图片url
            image_urls = [seg.url for seg in image_segments if seg.url]
            if not image_urls:
                yield event.plain_result("无法获取图片URL")
                return
            
            #  下载所有图片
            image_data_list = []
            for url in image_urls:
                image_data = await self._download_image(url)
                if image_data:
                    image_data_list.append(image_data)
            if not image_data_list:
                yield event.plain_result("图片下载失败")
                return
            
            #  对所有图片解混淆
            decrypted_images = []
            for image_data in image_data_list:
                try:
                    decrypted_image = await self._decrypt_image(image_data)
                    decrypted_images.append(decrypted_image)
                except Exception as e:
                    logger.error(f"图片解混淆处理失败: {str(e)}")
                    # 不是立即返回，而是继续处理其他图片
            if not decrypted_images:
                yield event.plain_result("所有图片解混淆处理失败，请检查图片格式是否正确")
                return
            
            #  构建转发信息
            client = event.bot
            bot_info = await client.api.call_action("get_login_info")
            msg_data = {
                "user_id": bot_info["user_id"],
                "raw_message": [Comp.Image.fromBytes(img) for img in decrypted_images],
                "time": int(time.time()),
                "sender": {"nickname": "318891403"}
            }
            
            forward_manager = ForwardManager(event)
            node = await forward_manager.build_base_node(msg_data)
            await client.api.call_action(
                "send_private_forward_msg",
                user_id=event.get_sender_id(),
                messages=[node]
            )
            event.should_call_llm(False)
            event.stop_event()

        except Exception as e:
            logger.error(f"解混淆命令处理出错: {str(e)}", exc_info=True)
            yield event.plain_result("处理图片时发生错误")
            event.stop_event()

    @staticmethod
    async def _download_image(url: str) -> bytes | None:
        url = url.replace("https://", "http://")
        try:
            async with aiohttp.ClientSession() as client:
                response = await client.get(url)
                img_bytes = await response.read()
                return img_bytes
        except Exception as e:
            logger.error(f"图片下载失败: {e}")
            return None

    async def terminate(self):
        logger.info("番茄图片解混淆插件已卸载")
