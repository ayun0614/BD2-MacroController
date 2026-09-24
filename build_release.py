import os
import shutil
import subprocess
import sys
import zipfile

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def build():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    dist_dir = os.path.join(base_dir, "dist")
    build_dir = os.path.join(base_dir, "build")
    output_app_dir = os.path.join(dist_dir, "BD2Controller")
    zip_path = os.path.join(dist_dir, "BD2Controller_Release.zip")

    print("=" * 60)
    print("[시작] BD2 Controller 원클릭 배포 패키지 빌드를 시작합니다.")
    print("=" * 60)

    # 1. PyInstaller 실행 명령어 구성
    icon_path = os.path.join(base_dir, "static", "favicon.ico")
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--onedir",
        "--console",
        "--name",
        "BD2Controller",
    ]
    if os.path.exists(icon_path):
        cmd.extend(["--icon", icon_path])

    cmd.append("app.py")

    print(f"\n[1/4] PyInstaller 바이너리 빌드 실행 중...")
    result = subprocess.run(cmd, cwd=base_dir)
    if result.returncode != 0:
        print("\n[오류] PyInstaller 빌드에 실패했습니다.")
        sys.exit(result.returncode)

    print("\n[2/4] 바이너리 컴파일 완료! 필수 데이터 및 리소스 복사를 진행합니다.")

    # 2. 필수 파일 및 디렉토리 복사
    files_to_copy = [
        "adb.exe",
        "AdbWinApi.dll",
        "config.yaml",
        "database.db",
    ]
    dirs_to_copy = [
        "template",
        "static",
        "costumes",
        "Tesseract-OCR",
    ]
    empty_dirs_to_create = [
        "backup",
        "unrecognized",
    ]

    for fname in files_to_copy:
        src = os.path.join(base_dir, fname)
        dst = os.path.join(output_app_dir, fname)
        if fname == "config.yaml" and os.path.exists(src):
            try:
                import yaml

                with open(src, "r", encoding="utf-8") as f:
                    cfg = yaml.safe_load(f) or {}
                cfg["adb_device"] = ""
                cfg["target_costumes"] = []
                cfg["discord"] = {
                    "enabled": False,
                    "bot_token": "",
                    "channel_id": "",
                    "user_id": "",
                    "share_error": False,
                    "share_complete": False,
                }
                with open(dst, "w", encoding="utf-8") as f:
                    yaml.dump(
                        cfg,
                        f,
                        allow_unicode=True,
                        default_flow_style=False,
                        sort_keys=False,
                    )
                print("  - 파일 복사 (개인정보/희망코스튬 보안 초기화): config.yaml")
                continue
            except Exception as e:
                print(f"  [경고] config.yaml 보안 초기화 실패 ({e}), 일반 복사 진행")

        if os.path.exists(src):
            shutil.copy2(src, dst)
            print(f"  - 파일 복사: {fname}")
        else:
            print(f"  [경고] 파일 누락: {fname}")

    for dname in dirs_to_copy:
        src = os.path.join(base_dir, dname)
        dst = os.path.join(output_app_dir, dname)
        if os.path.exists(src):
            if os.path.exists(dst):
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
            print(f"  - 폴더 복사: {dname}/")
        else:
            print(f"  [경고] 폴더 누락: {dname}/")

    for edname in empty_dirs_to_create:
        target = os.path.join(output_app_dir, edname)
        os.makedirs(target, exist_ok=True)
        print(f"  - 작업 폴더 생성: {edname}/")

    # 3. 간단한 사용 안내 README.txt 생성
    readme_path = os.path.join(output_app_dir, "README.txt")
    readme_content = """============================================================
  BD2 Controller - 브라운더스트2 매크로 컨트롤러
============================================================

[실행 방법]
1. 앱플레이어(LD플레이어, 뮤뮤, 녹스 등)를 실행하고 게임에 접속합니다.
2. 'BD2Controller.exe'를 더블 클릭하여 실행합니다.
3. 콘솔 창과 함께 기본 웹 브라우저에 컨트롤러 패널이 자동으로 열립니다.
   (자동으로 열리지 않을 경우 브라우저 주소창에 http://127.0.0.1:5000 입력)
4. 웹 UI에서 ADB 포트 번호(기본 5555)와 해상도를 확인하고 [매크로 시작]을 누릅니다.

[주의 사항]
- 본 폴더 안의 파일들(database.db, costumes, Tesseract-OCR 등)을 임의로 삭제하지 마세요.
- 종료 시에는 웹 UI에서 [매크로 중지]를 누른 뒤 콘솔 창을 닫아주시면 됩니다.
============================================================
"""
    with open(readme_path, "w", encoding="utf-8") as f:
        f.write(readme_content)
    print("  - 안내문 생성: README.txt")

    # 4. 배포용 ZIP 파일 생성
    print("\n[3/4] 배포용 ZIP 압축 파일 생성 중...")
    if os.path.exists(zip_path):
        os.remove(zip_path)

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
        for root, dirs, files in os.walk(output_app_dir):
            for file in files:
                file_full_path = os.path.join(root, file)
                rel_path = os.path.relpath(file_full_path, dist_dir)
                zipf.write(file_full_path, rel_path)

    zip_size_mb = os.path.getsize(zip_path) / (1024 * 1024)
    print(f"\n[4/4] 배포용 ZIP 압축 완료! ({zip_size_mb:.2f} MB)")
    print(f"  - 압축 파일 경로: {zip_path}")
    print(f"  - 배포 폴더 경로: {output_app_dir}")
    print("\n" + "=" * 60)
    print("[완료] 모든 빌드 및 패키징 작업이 성공적으로 완료되었습니다!")
    print("=" * 60)


if __name__ == "__main__":
    build()
