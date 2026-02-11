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



## 12) UNMATCHED가 많아질 때 개선 팁 (이번 버전에 반영)
- 기존처럼 `UNMATCHED 마진`만 조절하면,
  - 마진↑: 오분류는 줄지만 `UNMATCHED` 증가
  - 마진↓: `UNMATCHED`는 줄지만 오분류 증가
- 그래서 본 버전은 **2단계 판정**을 사용합니다.
  1. **Strict 판정**: 마진 조건 + 유형별 적응 임계치(템플릿 분포 기반) 통과 시 즉시 확정
  2. **Rescue 합의 판정**: strict 실패여도, 여러 뷰(01/02/03)가 같은 유형으로 합의하고 점수가 임계치 근처면 확정
- UI 파라미터
  - `Rescue 최소뷰`: 합의에 필요한 최소 뷰 수 (권장 2)
  - `Rescue 허용배수`: 유형별 임계치 대비 허용 배수 (권장 1.08~1.15)

### 추천 튜닝 순서
1. `UNMATCHED 마진`은 0.03~0.05 범위로 유지
2. `Rescue 최소뷰=2`, `Rescue 허용배수=1.10~1.15`로 시작
3. 여전히 UNMATCHED가 많으면 허용배수를 +0.02씩 증가
4. 오분류가 늘면 허용배수를 -0.02 하거나 최소뷰를 3으로 상향


## 13) UNMATCHED 원인 확인 (이번 버전)
`details.csv`에 아래 디버깅 컬럼이 추가됩니다.
- `decision_mode`: strict / rescue_consensus / unmatched / no_feature
- `unmatched_reason`: UNMATCHED가 된 구체 원인 문자열
- `best_type`, `best_threshold`, `margin_value`
- `top_vote_type`, `top_vote_count`
- `type_tuning`

이 값으로 “마진 부족인지 / 유형 임계치 초과인지 / 뷰 합의 부족인지”를 바로 확인할 수 있습니다.

## 14) 특정 유형만 성능 저하 시 조정 방법
템플릿 루트 폴더에 `type_tuning.csv`를 만들어 유형별로 임계치를 개별 조정할 수 있습니다.
UI의 **유형 튜닝 CSV 열기/생성** 버튼을 누르면 파일이 자동 생성됩니다.

CSV 형식:
```csv
type_name,threshold_mult,rescue_mult,margin_bias
라벨훼손_구김,1.05,1.10,0.00
반사,0.95,1.00,0.01
```

- `threshold_mult`: 해당 유형 strict 임계치 배수 (크면 완화, 작으면 강화)
- `rescue_mult`: 해당 유형 rescue 임계치 배수
- `margin_bias`: 해당 유형에만 추가 마진 요구치(+면 더 보수적)

### 권장 운영 팁
1. 특정 유형에서 UNMATCHED가 과도하면 해당 유형 `threshold_mult`를 +0.03~0.08
2. 오분류가 늘면 `margin_bias`를 +0.01~0.03
3. 다뷰 합의는 좋은데 strict 탈락이 많으면 `rescue_mult`를 +0.03~0.07


## 15) 오류 대응: `COORDINATE 'LOWER' IS LESS THAN 'UPPER'`
일부 손상/비정상 해상도 이미지에서 PIL crop 좌표가 역전되며 발생할 수 있는 오류입니다.
최신 버전은 다음을 반영해 자동 회피합니다.
- 비정상 크기 이미지(0x0 등) 자동 스킵
- 블록/중앙 crop 좌표를 항상 안전 범위로 보정
- 특징 추출 예외 발생 시 해당 파일만 스킵하고 작업 계속

또한 `type_tuning.csv`의 값은 아래 범위로 자동 보정됩니다.
- `threshold_mult`: 0.2 ~ 3.0
- `rescue_mult`: 0.2 ~ 3.0
- `margin_bias`: -0.2 ~ 0.2

예시 `Noread_PB상품구겨짐,1.00,1.00,-0.0075` 는 정상 범위이며 사용 가능합니다.
