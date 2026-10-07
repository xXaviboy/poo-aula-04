import argparse
import csv
import ctypes
import math
import os
import shutil
import sys
import time
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone


# Classe abstrata que define a estrutura das métricas.
class Metrica(ABC):
    def __init__(self, nome, unidade):
        self.nome = nome
        self.unidade = unidade
        self.valor = None

    @abstractmethod
    def coletar(self):
        pass

    def linha_csv(self, data_hora):
        return [data_hora, self.nome, self.valor, self.unidade]


# Herança: CpuMetrica herda de Metrica.
class CpuMetrica(Metrica):
    def __init__(self, intervalo_amostra=1):
        super().__init__("CPU", "%")
        self.intervalo_amostra = intervalo_amostra

    def _ler_tempos(self):
        if sys.platform.startswith("linux"):
            with open("/proc/stat", encoding="utf-8") as arquivo:
                campos = arquivo.readline().split()

            if len(campos) < 9 or campos[0] != "cpu":
                raise RuntimeError(
                    "Não foi possível ler os tempos da CPU."
                )

            # guest e guest_nice já estão incluídos nos demais campos.
            tempos = [int(valor) for valor in campos[1:9]]
            total = sum(tempos)
            ocioso = tempos[3] + tempos[4]

            return total, ocioso

        if sys.platform == "win32":
            class FILETIME(ctypes.Structure):
                _fields_ = [
                    ("baixo", ctypes.c_uint32),
                    ("alto", ctypes.c_uint32)
                ]

            funcao = ctypes.WinDLL(
                "kernel32", use_last_error=True
            ).GetSystemTimes

            funcao.argtypes = [ctypes.POINTER(FILETIME)] * 3
            funcao.restype = ctypes.c_int

            ocioso = FILETIME()
            kernel = FILETIME()
            usuario = FILETIME()

            if not funcao(
                ctypes.byref(ocioso),
                ctypes.byref(kernel),
                ctypes.byref(usuario)
            ):
                raise ctypes.WinError(ctypes.get_last_error())

            def converter(tempo):
                return (tempo.alto << 32) | tempo.baixo

            # No Windows, o tempo de kernel inclui o tempo ocioso.
            return (
                converter(kernel) + converter(usuario),
                converter(ocioso)
            )

        raise RuntimeError(
            "A coleta de CPU suporta Linux e Windows."
        )

    def coletar(self):
        total_antes, ocioso_antes = self._ler_tempos()

        time.sleep(self.intervalo_amostra)

        total_depois, ocioso_depois = self._ler_tempos()

        total = total_depois - total_antes
        ocioso = ocioso_depois - ocioso_antes

        if total <= 0:
            raise RuntimeError(
                "Não foi possível calcular o uso da CPU."
            )

        porcentagem = 100 * (1 - ocioso / total)

        self.valor = round(max(0, min(100, porcentagem)), 2)
        return self.valor


class MemoriaMetrica(Metrica):
    def __init__(self):
        super().__init__("Memoria", "MB")

    def coletar(self):
        if sys.platform.startswith("linux"):
            dados = {}

            with open("/proc/meminfo", encoding="utf-8") as arquivo:
                for linha in arquivo:
                    nome, conteudo = linha.split(":", 1)
                    dados[nome] = int(conteudo.split()[0])

            if "MemTotal" not in dados or "MemAvailable" not in dados:
                raise RuntimeError(
                    "Não foi possível ler o uso da memória."
                )

            # Converte os valores de KiB para bytes.
            usados = (
                dados["MemTotal"] - dados["MemAvailable"]
            ) * 1024

        elif sys.platform == "win32":
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_uint32),
                    ("dwMemoryLoad", ctypes.c_uint32),
                    ("ullTotalPhys", ctypes.c_uint64),
                    ("ullAvailPhys", ctypes.c_uint64),
                    ("ullTotalPageFile", ctypes.c_uint64),
                    ("ullAvailPageFile", ctypes.c_uint64),
                    ("ullTotalVirtual", ctypes.c_uint64),
                    ("ullAvailVirtual", ctypes.c_uint64),
                    ("ullAvailExtendedVirtual", ctypes.c_uint64)
                ]

            memoria = MEMORYSTATUSEX()
            memoria.dwLength = ctypes.sizeof(memoria)

            funcao = ctypes.WinDLL(
                "kernel32", use_last_error=True
            ).GlobalMemoryStatusEx

            funcao.argtypes = [ctypes.POINTER(MEMORYSTATUSEX)]
            funcao.restype = ctypes.c_int

            if not funcao(ctypes.byref(memoria)):
                raise ctypes.WinError(ctypes.get_last_error())

            usados = memoria.ullTotalPhys - memoria.ullAvailPhys

        else:
            raise RuntimeError(
                "A coleta de memória suporta Linux e Windows."
            )

        # Converte bytes para MB.
        self.valor = round(usados / 1_000_000, 2)
        return self.valor


class DiscoMetrica(Metrica):
    def __init__(self, caminho):
        super().__init__("Disco", "MB")
        self.caminho = caminho

    def coletar(self):
        disco = shutil.disk_usage(self.caminho)

        # Espaço livre em MB.
        self.valor = round(disco.free / 1_000_000, 2)
        return self.valor


def ler_argumentos():
    parser = argparse.ArgumentParser(
        description="Coleta métricas do sistema e grava um arquivo CSV."
    )

    parser.add_argument(
        "--metricas",
        nargs="+",
        choices=["cpu", "memoria", "disco"],
        default=["cpu", "memoria", "disco"],
        help="Métricas escolhidas. Por padrão, coleta todas."
    )

    parser.add_argument(
        "--iteracoes",
        type=int,
        default=1,
        help="Quantidade de coletas. Padrão: 1."
    )

    parser.add_argument(
        "--intervalo",
        type=float,
        default=5,
        help="Intervalo entre coletas, em segundos. Padrão: 5."
    )

    parser.add_argument(
        "--arquivo",
        default="metricas.csv",
        help="Arquivo CSV de saída. Padrão: metricas.csv."
    )

    parser.add_argument(
        "--caminho-disco",
        default=os.path.abspath(os.sep),
        help="Caminho da partição cujo espaço livre será medido."
    )

    args = parser.parse_args()

    if args.iteracoes < 1:
        parser.error(
            "--iteracoes deve ser um número inteiro positivo."
        )

    if not math.isfinite(args.intervalo) or args.intervalo <= 0:
        parser.error(
            "--intervalo deve ser um número positivo e finito."
        )

    if not args.arquivo.strip():
        parser.error("Informe um nome para o arquivo de saída.")

    return args


def executar_coletas(metricas, iteracoes, intervalo, caminho_csv):
    cabecalho = ["datetime", "metrica", "valor", "unidade"]

    tem_conteudo = (
        os.path.exists(caminho_csv)
        and os.path.getsize(caminho_csv) > 0
    )

    # Confere o formato do arquivo existente.
    if tem_conteudo:
        with open(
            caminho_csv, "r", newline="", encoding="utf-8"
        ) as arquivo:
            if next(csv.reader(arquivo), None) != cabecalho:
                raise ValueError(
                    "O CSV existente possui outro cabeçalho."
                )

    with open(
        caminho_csv, "a", newline="", encoding="utf-8"
    ) as arquivo:
        escritor = csv.writer(arquivo)

        if not tem_conteudo:
            escritor.writerow(cabecalho)

        for numero in range(1, iteracoes + 1):
            inicio = time.monotonic()

            # Polimorfismo: cada objeto executa seu próprio coletar().
            for metrica in metricas:
                metrica.coletar()

            # Horário de Brasília direto.
            data_hora = datetime.now(timezone(timedelta(hours=-3))).strftime("%Y-%m-%d %H:%M:%S")

            escritor.writerows(
                metrica.linha_csv(data_hora)
                for metrica in metricas
            )

            arquivo.flush()

            print(
                f"\nColeta {numero}/{iteracoes}"
                f" - {data_hora} (Brasília)"
            )

            for metrica in metricas:
                print(
                    f"{metrica.nome}: "
                    f"{metrica.valor:.2f} {metrica.unidade}"
                )

            if numero < iteracoes:
                tempo_gasto = time.monotonic() - inicio
                time.sleep(max(0, intervalo - tempo_gasto))

    print(
        f"\nColetas salvas em: {os.path.abspath(caminho_csv)}"
    )


def main():
    args = ler_argumentos()

    disponiveis = {
        "cpu": CpuMetrica(min(1.0, args.intervalo)),
        "memoria": MemoriaMetrica(),
        "disco": DiscoMetrica(args.caminho_disco)
    }

    # Remove métricas repetidas, mantendo a ordem escolhida.
    metricas = [
        disponiveis[nome]
        for nome in dict.fromkeys(args.metricas)
    ]

    try:
        executar_coletas(
            metricas,
            args.iteracoes,
            args.intervalo,
            args.arquivo
        )

    except KeyboardInterrupt:
        print(
            "\nColeta interrompida. "
            "Os registros gravados foram preservados."
        )
        return 130

    except (OSError, ValueError, RuntimeError) as erro:
        print(f"Erro: {erro}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())