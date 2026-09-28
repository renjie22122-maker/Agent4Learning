using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;

public static class AgentFolderPicker {
 [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
 [ComImport,Guid("d57c7288-d4ad-4768-be02-9d969532d960"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
 interface Dialog {
  [PreserveSig] int Show(IntPtr owner);
  void SetFileTypes(uint count,IntPtr specs); void SetFileTypeIndex(uint index); void GetFileTypeIndex(out uint index);
  void Advise(IntPtr events,out uint cookie);void Unadvise(uint cookie);void SetOptions(uint options);void GetOptions(out uint options);
  void SetDefaultFolder(Item item);void SetFolder(Item item);void GetFolder(out Item item);void GetCurrentSelection(out Item item);
  void SetFileName([MarshalAs(UnmanagedType.LPWStr)] string value);void GetFileName(out IntPtr value);
  void SetTitle([MarshalAs(UnmanagedType.LPWStr)] string value);void SetOkButtonLabel([MarshalAs(UnmanagedType.LPWStr)] string value);
  void SetFileNameLabel([MarshalAs(UnmanagedType.LPWStr)] string value);void GetResult(out Item item);void AddPlace(Item item,uint alignment);
  void SetDefaultExtension([MarshalAs(UnmanagedType.LPWStr)] string value);void Close(int result);void SetClientGuid(ref Guid guid);void ClearClientData();void SetFilter(IntPtr filter);
  void GetResults(out Items items);void GetSelectedItems(out Items items);
 }
 [ComImport,Guid("43826d1e-e718-42ee-bc55-a1e261c37bfe"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
 interface Item {
  void BindToHandler(IntPtr context,ref Guid handler,ref Guid iid,out IntPtr result);void GetParent(out Item item);
  void GetDisplayName(uint format,out IntPtr value);void GetAttributes(uint mask,out uint value);void Compare(Item item,uint hint,out int order);
 }
 [ComImport,Guid("b63ea76d-1f85-456f-a19c-48159efa858b"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
 interface Items {
  void BindToHandler(IntPtr context,ref Guid handler,ref Guid iid,out IntPtr result);void GetPropertyStore(uint flags,ref Guid iid,out IntPtr result);
  void GetPropertyDescriptionList(IntPtr key,ref Guid iid,out IntPtr result);void GetAttributes(uint flags,uint mask,out uint value);
  void GetCount(out uint count);void GetItemAt(uint index,out Item item);void EnumItems(out IntPtr result);
 }
 public static string[] Select() {
  Dialog dialog=(Dialog)Activator.CreateInstance(Type.GetTypeFromCLSID(new Guid("DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7")));
  try {
   dialog.SetOptions(0x20|0x200|0x40|0x800|0x8);
   dialog.SetTitle("选择项目文件夹（Ctrl / Shift 可多选）");dialog.SetOkButtonLabel("选择文件夹");
   int result=dialog.Show(GetForegroundWindow());if(result==unchecked((int)0x800704C7))return new string[0];Marshal.ThrowExceptionForHR(result);
   Items items;dialog.GetResults(out items);var paths=new List<string>();
   try {uint count;items.GetCount(out count);for(uint i=0;i<count;i++){Item item;items.GetItemAt(i,out item);try{IntPtr value;item.GetDisplayName(0x80058000,out value);try{paths.Add(Marshal.PtrToStringUni(value));}finally{Marshal.FreeCoTaskMem(value);}}finally{Marshal.ReleaseComObject(item);}}}
   finally{Marshal.ReleaseComObject(items);}return paths.ToArray();
  }finally{Marshal.ReleaseComObject(dialog);}
 }
}
