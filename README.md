Small pfSense utility that will print Wifi Vouchers to a esc/POS thermal printer.
Go to PfSense webGUI/Captive Portal and create Wifi Vouchers then choose menu option to export to a CSV file and save the file into the same directory as WifiVouchers.py
Launch WifiVouchers and simply click on Print Voucher. A messagebox will prompt you to confirm the voucher printed successfully (in case of paper jam etc.).
Clicking YES will delete the voucher number from the file and the next time a new voucher number will be printed. Clicking NO will not remove the voucher number from the csv file and instead allow you to print the last voucher number.
The ini file must be present in the current directory. The name of the printer must match the name of the esc/POS printer listed in the Windows Printer & scanners list.
