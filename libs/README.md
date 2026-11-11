# libs/ : dc_shell_sim 셀 라이브러리 폴더

이 폴더 아래의 각 하위 폴더는 가상 리눅스에서 `/tools/lib/<폴더이름>`으로 보입니다(읽기 전용).
`<폴더>/lib/*.lib` 파일마다 `/tools/lib/<폴더>/db/*.db`가 자동으로 만들어집니다.

## 내 라이브러리 추가하기

1. 아래와 같이 폴더를 만들고 liberty 파일을 넣습니다. (예: 학교 실습용 SAED32, Nangate45)
   ```
   libs/my_lib/lib/my_lib_tt.lib
   ```
2. 터미널을 다시 실행하면 `ls /tools/lib/my_lib/db`에 `my_lib_tt.db`가 보입니다.
3. `.synopsys_dc.setup` 또는 dc_shell에서 다음과 같이 설정합니다.
   ```
   set_app_var search_path    "$search_path /tools/lib/my_lib/db"
   set_app_var target_library my_lib_tt.db
   set_app_var link_library   "* $target_library"
   ```

`.lib` 파일을 복사하지 않고 직접 지정해도 됩니다.
예: `set_app_var target_library /mnt/c/Users/me/libs/NangateOpenCellLibrary_typical.lib`

- 셀 기능(function)을 진리표로 분석해서 INV / NAND2 / NOR2 / AOI21 / OAI21 / MUX2 / XOR / FA / HA / DFF / DFF(reset) / ICG를 자동으로 찾습니다.
- 찾지 못한 기능은 다른 셀 조합으로 분해합니다. 어떤 셀이 매칭됐는지는 `check_library`, `report_lib <lib>`로 확인할 수 있습니다.
- 라이선스가 있는 라이브러리(SAED 등)는 본인 PC에서만 사용하세요.
