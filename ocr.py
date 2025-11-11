import os
import re
import cv2
import pytesseract
from PIL import Image
import requests


def preprocess_image(image_path):
    image = cv2.imread(image_path)
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    gray = cv2.convertScaleAbs(gray, alpha=1.5, beta=0)
    gray = cv2.medianBlur(gray, 3)
    _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return thresh


def extract_total_amount(text):
    patterns = [
        r'(?:итого|всего|total|сумма|к\s*оплате|рубли)[:\s]*([0-9]+[.,]\d{2})',
        r'([0-9]+[.,]\d{2})\s*(?:руб|р|₽|usd|\$|€|£)',
        r'([0-9]+[.,]\d{2})\s*$',
        r'^.*?([0-9]+[.,]\d{2})\s*$'
    ]

    lines = text.split('\n')
    amounts = []

    for line in lines:
        line = line.lower().strip()
        if not line:
            continue

        for pattern in patterns:
            matches = re.findall(pattern, line)
            for match in matches:
                amount_str = match.replace(',', '.')
                try:
                    amount = float(amount_str)
                    amounts.append((amount, line))
                except ValueError:
                    continue

    if amounts:
        amounts.sort(key=lambda x: x[0], reverse=True)
        return amounts[0][0]

    return None


def process_receipt(image_path):
    try:
        processed_image = preprocess_image(image_path)

        cv2.imwrite('img/processed_receipt.jpg', processed_image)

        configs = [
            '--psm 6',
            '--psm 4',
            '--psm 3',
        ]

        all_text = ""

        for config in configs:
            text = pytesseract.image_to_string(processed_image, config=config, lang='rus+eng')
            all_text += text + "\n"

        pil_image = Image.fromarray(processed_image)
        text_pil = pytesseract.image_to_string(pil_image, lang='rus+eng')
        all_text += text_pil

        total_amount = extract_total_amount(all_text)

        if total_amount:
            return total_amount
        else:
            return None

    except Exception as e:
        print(f"Ошибка при обработке изображения: {e}")
        return None


def extract_amounts_with_context(text):
    lines = text.split('\n')
    amount_candidates = []

    for i, line in enumerate(lines):
        line_lower = line.lower().strip()

        amounts_in_line = re.findall(r'(\d+[.,]\d{2})', line)
        for amount_str in amounts_in_line:
            amount = float(amount_str.replace(',', '.'))

            context_score = 0
            total_keywords = ['итого', 'всего', 'total', 'сумма', 'оплат', 'итог', 'рубли']
            for keyword in total_keywords:
                if keyword in line_lower:
                    context_score += 3

            if i >= len(lines) - 2:
                context_score += 2
            if any(char in line for char in ['=', '-', '_'] * 3):
                context_score += 1
            amount_candidates.append({
                'amount': amount,
                'line': line,
                'line_number': i,
                'score': context_score
            })

    if amount_candidates:
        amount_candidates.sort(key=lambda x: (-x['score'], -x['amount']))
        return amount_candidates[0]['amount']
    return None


def get_total_by_url(url):
    try:
        response = requests.get(url, stream=True)
        os.makedirs('img', exist_ok=True)
        image_path = 'img/image.jpg'
        with open(image_path, 'wb') as file:
            for chunk in response.iter_content(chunk_size=8192):
                file.write(chunk)
        total = process_receipt(image_path)
        return total
    except requests.exceptions.RequestException as e:
        print(f"Error downloading image: {e}")
        return None
