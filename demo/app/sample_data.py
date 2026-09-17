# -*- coding: utf-8 -*-
"""演示用样本数据。

全部是自编的通用界面文案，不涉及任何真实客户或项目数据。
每条的字段与上游契约一致：string_id / source / mt（机器译文）。
故意混入几种"坏样本"，好让标签校验和重译路径都能被走到。
"""

import random

BASE_PAIRS = [
    ("Welcome to our store", "Bienvenido a nuestra tienda"),
    ("Your order has been shipped", "Su pedido ha sido enviado"),
    ("Please enter your email address", "Por favor introduzca su direccion de correo"),
    ("The file could not be uploaded", "El archivo no se pudo cargar"),
    ("Settings saved successfully", "Configuracion guardada correctamente"),
    ("This action cannot be undone", "Esta accion no se puede deshacer"),
    ("Contact support for further assistance", "Contacte con soporte para mas ayuda"),
    ("Select a payment method to continue", "Seleccione un metodo de pago para continuar"),
    ("Your session will expire in 5 minutes", "Su sesion expirara en 5 minutos"),
    ("Download the report as a PDF", "Descargue el informe como PDF"),
    ("Search results for your query", "Resultados de busqueda para su consulta"),
    ("No items match the current filter", "Ningun articulo coincide con el filtro"),
]

# 带行内占位符的句子：标签保真校验专治这类
TAGGED_SAMPLES = [
    ("Welcome back, {1}! You have {2} new messages.",
     "Bienvenido de nuevo, {1}! Tiene {2} mensajes nuevos.", None),
    ("Order {1} was delivered on {2}.", "El pedido {1} se entrego el {2}.", None),
    # 标签数量丢失 —— 必须判 N 并重译
    ("Your balance is {1} and your points are {2}.",
     "Su saldo es y sus puntos son {2}.", "tag_loss"),
    # 标签编号变了 —— 数量对但编号集合不一致
    ("Download {1} from {2}.", "Descargue {2} desde {2}.", "tag_id_change"),
    # 嵌套标签配对错乱
    ("Click <b>Save</b> to apply <i>changes</i>.",
     "Haga clic en <b>Guardar</b> para aplicar cambios</i>.", "tag_unbalanced"),
    # 转义形式被改动
    ("Use &amp; to separate values.", "Use & para separar valores.", "escape_change"),
    # 空机译 —— 上游没给 MT，需要生成新译文
    ("Enable two-factor authentication for better security.", "", "empty_mt"),
]

# 禁改词：出现在 source 时译文必须原样保留
DNT_WORDS = ["API", "SDK", "cloud"]


def make_segments(n, seed=42, with_tags=True, inject_bad=True):
    """生成 n 条 segment。

    inject_bad 控制是否掺入坏样本（标签缺损 / 空机译）。
    演示正常流水线时关掉，演示兜底路径时打开。
    """
    rnd = random.Random(seed)
    out = []
    pool = list(BASE_PAIRS)
    if with_tags:
        pool += [(s, m) for s, m, _ in TAGGED_SAMPLES]

    for i in range(n):
        if with_tags and inject_bad and i % 7 == 3 and i // 7 < len(TAGGED_SAMPLES):
            src, mt, _kind = TAGGED_SAMPLES[i // 7]
        else:
            src, mt = pool[rnd.randrange(len(pool))]
            if with_tags and rnd.random() < 0.25:
                src = src + " {1}"
                mt = mt + " {1}"
        sid = "seg-%05d" % (i + 1)
        out.append({
            "string_id": sid,
            "source": "%s [%d]" % (src, i + 1) if with_tags else src,
            "mt": mt,
        })
    return out


def make_mixed_workload():
    """构造混合负载：1 个大任务 + 20 个小任务。

    这个大任务取 790 段，是为了复现生产上那次队列饥饿事故的规模量级。
    """
    big = make_segments(790, seed=7, inject_bad=False)
    small = [make_segments(5, seed=100 + i, inject_bad=False) for i in range(20)]
    return {"big": big, "small": small}
