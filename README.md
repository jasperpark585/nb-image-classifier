# NB Image Classifier (Windows Desktop, Tkinter)

쿠팡 CBS 소터 스캐너 이미지에서 `NB`(No Barcode) 케이스를 **상품 단위(01/02/03 묶음)**로 분류/집계하는 데스크톱 앱입니다.

## 1) MVP 범위 (1차 목표)
- NB 파일 스캔
- 그룹키 묶기: `_01_NB_`, `_02_NB_`, `_03_NB_` → `_NB_`
- 템플릿 기반 2단계 분류(coarse + fine)
- `details.csv`, `summary.csv` 생성
- 결과 이미지를 `yyyymmdd/유형명/` 폴더로 이동 정리
- UI: 폴더 선택 / 템플릿 일괄 등록 / 실행 / 취소 / 로그

## 2) 폴더 구조
아래 3개 파일만 있으면 됩니다.

```text
nb-image-classifier/
 ├─ nb_image_classifier.py
 ├─ requirements.txt
 └─ README.md
```

## 3) 초보자용 설치/실행 (Windows)

### 3-1. Python 설치
1. Python 3.11.x 설치 (공식 설치 파일 사용)
2. 설치 화면에서 **Add python.exe to PATH** 체크
3. 설치 확인:
   ```bat
   py -3.11 --version
   ```

### 3-2. 가상환경 + 패키지 설치
프로젝트 폴더(위 3개 파일 있는 폴더)에서:

```bat
py -3.11 -m venv .venv
.venv\Scripts\activate
py -3.11 -m pip install --upgrade pip
pip install -r requirements.txt
```

### 3-3. 실행
```bat
py -3.11 nb_image_classifier.py
```

## 4) 사용 방법 (UI)
1. **검사 폴더** 선택
2. **템플릿 루트 폴더** 선택
   - 예: `base_folder\라벨훼손\구김\xxx.jpg`
   - 유형명 자동 생성: `라벨훼손_구김`
3. 템플릿 등록
   - `미리보기 등록(최대 1000)` : 빠른 확인용
   - `폴더 전체 자동 등록` : 1000장 이상 전체 등록
4. 옵션 설정
   - `동일 그룹의 NB 없는(OK) 이미지도 포함` (확장 옵션)
   - `전처리(auto-contrast/gamma) 사용`
   - `유형당 최대 템플릿`, `coarse 후보 Top-K`, `UNMATCHED 마진`
5. **실행**
6. 중단 필요 시 **취소** 버튼

## 5) 출력물
검사 폴더 아래에 실행일자 폴더 생성:

```text
<scan_folder>/YYYYMMDD/
 ├─ details.csv
 ├─ summary.csv
 ├─ 유형A/
 ├─ 유형B/
 └─ UNMATCHED/
```

- `details.csv`
  - group_key, result_type, raw_result_type, distance_mean, distance_second, used_view_count, used_paths
- `summary.csv`
  - result_type, count

> `ABC_01`, `ABC_02` 같이 끝 번호가 붙은 유형은 집계 시 `ABC`로 정규화됩니다.

## 6) 핵심 로직 설명 (정확도 + 속도)

### 정확도 강화 포인트
- 어두운 이미지 대응 전처리
  - grayscale + auto-contrast + gamma + contrast
- 특징 2종 결합
  - coarse: 8x8 평균해시(매우 빠름)
  - fine: 16x16 해시 + 블록 통계 + 중앙 크롭 특징
- 3뷰(01/02/03) 결합
  - 뷰별 결과를 평균 거리로 합산해 상품 단위 최종 판정
- 애매하면 `UNMATCHED`
  - 1등/2등 점수 차가 작으면 강제 분류 대신 보류

### 속도 강화 포인트
- 이미지 다운스케일(`max_dim=512`) 후 특징 추출
- 템플릿 특징 캐시(`~/.nb_image_classifier_feature_cache.json`)
  - 다음 실행부터 재계산 최소화
- 유형당 템플릿 수 샘플링 제한
- 2단계 검색
  1. coarse centroid로 Top-K 유형만 선택
  2. 후보에 대해서만 fine 비교
- ThreadPoolExecutor로 그룹 병렬 처리

## 7) 설정/데이터 유지
앱 종료 후에도 아래 파일에 유지됩니다.
- 설정 + 템플릿 목록: `~/.nb_image_classifier_config.json`
- 템플릿/쿼리 특징 캐시: `~/.nb_image_classifier_feature_cache.json`

## 8) EXE 빌드 (PyInstaller)

### 8-1. 빌드 도구 설치
```bat
.venv\Scripts\activate
pip install pyinstaller
```

### 8-2. 권장 빌드 (McAfee 오탐 완화: onedir + no UPX)
```bat
py -3.11 -m PyInstaller --noconfirm --clean --windowed --onedir --noupx --name NBImageClassifier nb_image_classifier.py
```

산출물:
- `dist\NBImageClassifier\NBImageClassifier.exe`

### 8-3. onefile (선택)
```bat
py -3.11 -m PyInstaller --noconfirm --clean --windowed --onefile --noupx --name NBImageClassifier nb_image_classifier.py
```

## 9) 배포 방법
1. `dist\NBImageClassifier\` 폴더 전체를 ZIP 압축
2. USB/공유폴더로 대상 PC 전달
3. 대상 PC에서 압축 해제 후 `.exe` 실행

## 10) McAfee/백신 오탐 대응 가이드
사내 정책 범위 내에서 아래 순서 권장:
1. **onedir 우선 사용** (`onefile`보다 오탐 확률이 낮은 편)
2. `--noupx` 유지 (압축 바이너리 오탐 완화)
3. 실행파일 해시(SHA256)와 빌드 로그 보관
4. 사내 보안팀에 **화이트리스트(예외 경로/해시) 등록** 요청
5. 가능하면 코드서명 인증서로 서명
6. 버전정보/아이콘/manifest를 포함해 “정식 배포물 형태” 유지

## 11) 2차 확장 로드맵
- PyInstaller spec 개선 (버전정보/아이콘/manifest)
- 캐시 무결성 검사 + 만료 정책
- 오탐/오분류 사례 수집 문서 + 템플릿 품질 가이드
- 선택적 비교 알고리즘(ORB/HOG/SSIM) 플러그인화
- 대량 데이터에서 batch I/O 최적화

