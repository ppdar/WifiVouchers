Small pfSense utility written in Python32 that will print Wifi Vouchers to a esc/POS thermal printer.
Go to PfSense webGUI/Captive Portal and create Wifi Vouchers then choose menu option to export to a CSV file and save the file into the same directory as WifiVouchers.exe. 
Launch WifiVouchers and simply click on Print Voucher. A messagebox will prompt you to confirm the voucher printed successfully (in case of paper jam etc.).
Clicking YES will delete the voucher number from the file and the next time a new voucher number will be printed. Clicking CANCEL will not delete the voucher from the csv file and instead allow you to print the last voucher number again.
The ini file must be present in the current directory. The name of the printer must match the name of the esc/POS printer listed in the Windows Printer & scanners list.
